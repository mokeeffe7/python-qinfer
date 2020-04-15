#!/usr/bin/python
# -*- coding: utf-8 -*-
##
# smc.py: Sequential Monte Carlo module
##
# © 2017, Chris Ferrie (csferrie@gmail.com) and
#         Christopher Granade (cgranade@cgranade.com).
#
# Redistribution and use in source and binary forms, with or without
# modification, are permitted provided that the following conditions are met:
#
#     1. Redistributions of source code must retain the above copyright
#        notice, this list of conditions and the following disclaimer.
#
#     2. Redistributions in binary form must reproduce the above copyright
#        notice, this list of conditions and the following disclaimer in the
#        documentation and/or other materials provided with the distribution.
#
#     3. Neither the name of the copyright holder nor the names of its
#        contributors may be used to endorse or promote products derived from
#        this software without specific prior written permission.
#
# THIS SOFTWARE IS PROVIDED BY THE COPYRIGHT HOLDERS AND CONTRIBUTORS "AS IS"
# AND ANY EXPRESS OR IMPLIED WARRANTIES, INCLUDING, BUT NOT LIMITED TO, THE
# IMPLIED WARRANTIES OF MERCHANTABILITY AND FITNESS FOR A PARTICULAR PURPOSE
# ARE DISCLAIMED. IN NO EVENT SHALL THE COPYRIGHT HOLDER OR CONTRIBUTORS BE
# LIABLE FOR ANY DIRECT, INDIRECT, INCIDENTAL, SPECIAL, EXEMPLARY, OR
# CONSEQUENTIAL DAMAGES (INCLUDING, BUT NOT LIMITED TO, PROCUREMENT OF
# SUBSTITUTE GOODS OR SERVICES; LOSS OF USE, DATA, OR PROFITS; OR BUSINESS
# INTERRUPTION) HOWEVER CAUSED AND ON ANY THEORY OF LIABILITY, WHETHER IN
# CONTRACT, STRICT LIABILITY, OR TORT (INCLUDING NEGLIGENCE OR OTHERWISE)
# ARISING IN ANY WAY OUT OF THE USE OF THIS SOFTWARE, EVEN IF ADVISED OF THE
# POSSIBILITY OF SUCH DAMAGE.
##

## FEATURES ###################################################################

from __future__ import absolute_import
from __future__ import division, unicode_literals

## ALL ########################################################################

# We use __all__ to restrict what globals are visible to external modules.
__all__ = [
    'SMCUpdater',
    'SMCUpdaterBCRB',
    'MixedApproximateSMCUpdater'
]

## IMPORTS ####################################################################

from builtins import map, zip

import warnings

import numpy as np

# from itertools import zip

from scipy.spatial import ConvexHull, Delaunay
import scipy.linalg as la
import scipy.stats
import scipy.interpolate
from scipy.ndimage.filters import gaussian_filter1d

from qinfer.abstract_model import DifferentiableModel
from qinfer.metrics import rescaled_distance_mtx
from qinfer.distributions import ParticleDistribution
import qinfer.resamplers
import qinfer.clustering
import qinfer.metrics
from qinfer.utils import outer_product, mvee, uniquify, format_uncertainty, \
    in_ellipsoid
from qinfer._exceptions import ApproximationWarning, ResamplerWarning

try:
    import matplotlib.pyplot as plt
except ImportError:
    import warnings
    warnings.warn("Could not import pyplot. Plotting methods will not work.")
    plt = None

try:
    import mpltools.special as mpls
except:
    # Don't even warn in this case.
    mpls = None

## LOGGING ####################################################################

import logging
logger = logging.getLogger(__name__)
logger.addHandler(logging.NullHandler())

## CLASSES #####################################################################

class SMCUpdater(ParticleDistribution):
    r"""
    Creates a new Sequential Monte carlo updater, using the algorithm of
    [GFWC12]_.

    :param Model model: Model whose parameters are to be inferred.
    :param int n_particles: The number of particles to be used in the particle approximation.
    :param Distribution prior: A representation of the prior distribution.
    :param callable resampler: Specifies the resampling algorithm to be used. See :ref:`resamplers`
        for more details.
    :param float resample_thresh: Specifies the threshold for :math:`N_{\text{ess}}` to decide when to resample.
    :param bool debug_resampling: If `True`, debug information will be
        generated on resampling performance, and will be written to the
        standard Python logger.
    :param bool track_resampling_divergence: If true, then the divergences
        between the pre- and post-resampling distributions are tracked and
        recorded in the ``resampling_divergences`` attribute.
    :param str zero_weight_policy: Specifies the action to be taken when the
        particle weights would all be set to zero by an update.
        One of ``["ignore", "skip", "warn", "error", "reset"]``.
    :param float zero_weight_thresh: Value to be used when testing for the
        zero-weight condition.
    :param bool canonicalize: If `True`, particle locations will be updated
        to canonical locations as described by the model class after each
        prior sampling and resampling.
    """
    def __init__(self,
            model, n_particles, prior,
            resample_a=None, resampler=None, resample_thresh=0.5,
            debug_resampling=False,
            track_resampling_divergence=False,
            zero_weight_policy='error', zero_weight_thresh=None,
            canonicalize=True
        ):
        super(SMCUpdater, self).__init__(
            particle_locations=np.zeros((0, model.n_modelparams)),
            particle_weights=np.zeros((0,))
        )

        # Initialize metadata on resampling performance.
        self._resample_count = 0
        self._min_n_ess = n_particles

        self.model = model
        self.prior = prior

        # Record whether we are to canonicalize or not.
        self._canonicalize = bool(canonicalize)

        ## RESAMPLER CONFIGURATION ##
        # Backward compatibility with the old resample_a keyword argument,
        # which assumed that the Liu and West resampler was being used.
        self._debug_resampling = debug_resampling
        if resample_a is not None:
            warnings.warn("The 'resample_a' keyword argument is deprecated; use 'resampler=LiuWestResampler(a)' instead.", DeprecationWarning)
            if resampler is not None:
                raise ValueError("Both a resample_a and an explicit resampler were provided; please provide only one.")
            self.resampler = qinfer.resamplers.LiuWestResampler(a=resample_a)
        else:
            if resampler is None:
                self.resampler = qinfer.resamplers.LiuWestResampler(default_n_particles=n_particles)
            else:
                self.resampler = resampler


        self.resample_thresh = resample_thresh

        # Initialize properties to hold information about the history.
        self._just_resampled = False
        self._data_record = []
        self._normalization_record = []
        self._resampling_divergences = [] if track_resampling_divergence else None

        self._zero_weight_policy = zero_weight_policy
        self._zero_weight_thresh = (
            zero_weight_thresh
            if zero_weight_thresh is not None else
            10 * np.spacing(1)
        )

        ## PARTICLE INITIALIZATION ##
        self.reset(n_particles)

    ## PROPERTIES #############################################################

    @property
    def resample_count(self):
        """
        Returns the number of times that the updater has resampled the particle
        approximation.

        :type: `int`
        """
        # We wrap this in a property to prevent external resetting and to enable
        # a docstring.
        return self._resample_count

    @property
    def just_resampled(self):
        """
        `True` if and only if there has been no data added since the last
        resampling, or if there has not yet been a resampling step.

        :type: `bool`
        """
        return self._just_resampled

    @property
    def normalization_record(self):
        """
        Returns the normalization record.

        :type: `float`
        """
        # We wrap this in a property to prevent external resetting and to enable
        # a docstring.
        return self._normalization_record

    @property
    def log_total_likelihood(self):
        """
        Returns the log-likelihood of all the data collected so far.

        Equivalent to::

            np.sum(np.log(updater.normalization_record))

        :type: `float`
        """
        return np.sum(np.log(self.normalization_record))

    @property
    def min_n_ess(self):
        """
        Returns the smallest effective sample size (ESS) observed in the
        history of this updater.

        :type: `float`
        :return: The minimum of observed effective sample sizes as
            reported by :attr:`~qinfer.SMCUpdater.n_ess`.
        """
        return self._min_n_ess

    @property
    def data_record(self):
        """
        List of outcomes given to :meth:`~SMCUpdater.update`.

        :type: `list` of `int`
        """
        # We use [:] to force a new list to be made, decoupling
        # this property from the caller.
        return self._data_record[:]

    @property
    def resampling_divergences(self):
        """
        List of KL divergences between the pre- and post-resampling
        distributions, if that is being tracked. Otherwise, `None`.

        :type: `list` of `float` or `None`
        """
        return self._resampling_divergences

    ## PRIVATE METHODS ########################################################

    def _maybe_resample(self):
        """
        Checks the resample threshold and conditionally resamples.
        """
        ess = self.n_ess
        if ess <= 10:
            warnings.warn(
                "Extremely small n_ess encountered ({}). "
                "Resampling is likely to fail. Consider adding particles, or "
                "resampling more often.".format(ess),
                ApproximationWarning
            )
        if ess < self.n_particles * self.resample_thresh:
            self.resample()
            pass

    ## INITIALIZATION METHODS #################################################

    def reset(self, n_particles=None, only_params=None, reset_weights=True):
        """
        Causes all particle locations and weights to be drawn fresh from the
        initial prior.

        :param int n_particles: Forces the size of the new particle set. If
            `None`, the size of the particle set is not changed.
        :param slice only_params: Resets only some of the parameters. Cannot
            be set if ``n_particles`` is also given.
        :param bool reset_weights: Resets the weights as well as the particles.
        """
        # Particles are stored using two arrays, particle_locations and
        # particle_weights, such that:
        #
        # particle_locations[idx_particle, idx_modelparam] is the idx_modelparam
        #     parameter of the particle idx_particle.
        # particle_weights[idx_particle] is the weight of the particle
        #     idx_particle.

        if n_particles is not None and only_params is not None:
            raise ValueError("Cannot set both n_particles and only_params.")

        if n_particles is None:
            n_particles = self.n_particles

        if reset_weights:
            self.particle_weights = np.ones((n_particles,)) / n_particles

        if only_params is None:
            sl = np.s_[:, :]
            # Might as well make a new array if we're resetting everything.
            self.particle_locations = np.zeros((n_particles, self.model.n_modelparams))
        else:
            sl = np.s_[:, only_params]

        self.particle_locations[sl] = self.prior.sample(n=n_particles)[sl]

        # Since this changes particle positions, we must recanonicalize.
        if self._canonicalize:
            self.particle_locations[sl] = self.model.canonicalize(self.particle_locations[sl])

    ## UPDATE METHODS #########################################################

    def hypothetical_update(self, outcomes, expparams, return_likelihood=False, return_normalization=False):
        """
        Produces the particle weights for the posterior of a hypothetical
        experiment.

        :param outcomes: Integer index of the outcome of the hypothetical
            experiment.
        :type outcomes: int or an ndarray of dtype int.
        :param numpy.ndarray expparams: Experiments to be used for the hypothetical
            updates.

        :type weights: ndarray, shape (n_outcomes, n_expparams, n_particles)
        :param weights: Weights assigned to each particle in the posterior
            distribution :math:`\Pr(\omega | d)`.
        """

        # It's "hypothetical", don't want to overwrite old weights yet!
        weights = self.particle_weights
        locs = self.particle_locations

        # Check if we have a single outcome or an array. If we only have one
        # outcome, wrap it in a one-index array.
        if not isinstance(outcomes, np.ndarray):
            outcomes = np.array([outcomes])

        # update the weights sans normalization
        # Rearrange so that likelihoods have shape (outcomes, experiments, models).
        # This makes the multiplication with weights (shape (models,)) make sense,
        # since NumPy broadcasting rules align on the right-most index.
        L = self.model.likelihood(outcomes, locs, expparams).transpose([0, 2, 1])
        hyp_weights = weights * L

        # Sum up the weights to find the renormalization scale.
        norm_scale = np.sum(hyp_weights, axis=2)[..., np.newaxis]

        # As a special case, check whether any entries of the norm_scale
        # are zero. If this happens, that implies that all of the weights are
        # zero--- that is, that the hypothicized outcome was impossible.
        # Conditioned on an impossible outcome, all of the weights should be
        # zero. To allow this to happen without causing a NaN to propagate,
        # we forcibly set the norm_scale to 1, so that the weights will
        # all remain zero.
        #
        # We don't actually want to propagate this out to the caller, however,
        # and so we save the "fixed" norm_scale to a new array.
        fixed_norm_scale = norm_scale.copy()
        fixed_norm_scale[np.abs(norm_scale) < np.spacing(1)] = 1

        # normalize
        norm_weights = hyp_weights / fixed_norm_scale
            # Note that newaxis is needed to align the two matrices.
            # This introduces a length-1 axis for the particle number,
            # so that the normalization is broadcast over all particles.
        if not return_likelihood:
            if not return_normalization:
                return norm_weights
            else:
                return norm_weights, norm_scale
        else:
            if not return_normalization:
                return norm_weights, L
            else:
                return norm_weights, L, norm_scale

    def update(self, outcome, expparams, check_for_resample=True):
        """
        Given an experiment and an outcome of that experiment, updates the
        posterior distribution to reflect knowledge of that experiment.

        After updating, resamples the posterior distribution if necessary.

        :param int outcome: Label for the outcome that was observed, as defined
            by the :class:`~qinfer.abstract_model.Model` instance under study.
        :param expparams: Parameters describing the experiment that was
            performed.
        :type expparams: :class:`~numpy.ndarray` of dtype given by the
            :attr:`~qinfer.abstract_model.Model.expparams_dtype` property
            of the underlying model
        :param bool check_for_resample: If :obj:`True`, after performing the
            update, the effective sample size condition will be checked and
            a resampling step may be performed.
        """

        # First, record the outcome.
        # TODO: record the experiment as well.
        self._data_record.append(outcome)
        self._just_resampled = False

        # Perform the update.
        weights, norm = self.hypothetical_update(outcome, expparams, return_normalization=True)

        # Check for negative weights before applying the update.
        if not np.all(weights >= 0):
            warnings.warn("Negative weights occured in particle approximation. Smallest weight observed == {}. Clipping weights.".format(np.min(weights)), ApproximationWarning)
            np.clip(weights, 0, 1, out=weights)

        # Next, check if we have caused the weights to go to zero, as can
        # happen if the likelihood is identically zero for all particles,
        # or if the previous clip step choked on a NaN.
        if np.sum(weights) <= self._zero_weight_thresh:
            if self._zero_weight_policy == 'ignore':
                pass
            elif self._zero_weight_policy == 'skip':
                return
            elif self._zero_weight_policy == 'warn':
                warnings.warn("All particle weights are zero. This will very likely fail quite badly.", ApproximationWarning)
            elif self._zero_weight_policy == 'error':
                raise RuntimeError("All particle weights are zero.")
            elif self._zero_weight_policy == 'reset':
                warnings.warn("All particle weights are zero. Resetting from initial prior.", ApproximationWarning)
                self.reset()
            else:
                raise ValueError("Invalid zero-weight policy {} encountered.".format(self._zero_weight_policy))

        # Since hypothetical_update returns an array indexed by
        # [outcome, experiment, particle], we need to strip off those two
        # indices first.
        self.particle_weights[:] = weights[0,0,:]

        # Record the normalization
        self._normalization_record.append(norm[0][0])

        # Update the particle locations according to the model's timestep.
        self.particle_locations = self.model.update_timestep(
            self.particle_locations, expparams
        )[:, :, 0]

        # Check if we need to update our min_n_ess attribute.
        if self.n_ess <= self._min_n_ess:
            self._min_n_ess = self.n_ess

        # Resample if needed.
        if check_for_resample:
            self._maybe_resample()

    def batch_update(self, outcomes, expparams, resample_interval=5):
        r"""
        Updates based on a batch of outcomes and experiments, rather than just
        one.

        :param numpy.ndarray outcomes: An array of outcomes of the experiments that
            were performed.
        :param numpy.ndarray expparams: Either a scalar or record single-index
            array of experiments that were performed.
        :param int resample_interval: Controls how often to check whether
            :math:`N_{\text{ess}}` falls below the resample threshold.
        """

        # TODO: write a faster implementation here using vectorized calls to
        #       likelihood.

        # Check that the number of outcomes and experiments is the same.
        n_exps = outcomes.shape[0]
        if expparams.shape[0] != n_exps:
            raise ValueError("The number of outcomes and experiments must match.")

        if len(expparams.shape) == 1:
            expparams = expparams[:, None]

        # Loop over experiments and update one at a time.
        for idx_exp, (outcome, experiment) in enumerate(zip(iter(outcomes), iter(expparams))):
            self.update(outcome, experiment, check_for_resample=False)
            if (idx_exp + 1) % resample_interval == 0:
                self._maybe_resample()

    ## RESAMPLING METHODS #####################################################

    def resample(self):
        """
        Forces the updater to perform a resampling step immediately.
        """

        if self.just_resampled:
            warnings.warn(
                "Resampling without additional data; this may not perform as "
                "desired.",
                ResamplerWarning
            )

        # Record that we have performed a resampling step.
        self._just_resampled = True
        self._resample_count += 1

        # If we're tracking divergences, make a copy of the weights and
        # locations.
        if self._resampling_divergences is not None:
            old_locs = self.particle_locations.copy()
            old_weights = self.particle_weights.copy()

        # Record the previous mean, cov if needed.
        if self._debug_resampling:
            old_mean = self.est_mean()
            old_cov = self.est_covariance_mtx()

        # Find the new particle locations according to the chosen resampling
        # algorithm.
        # We pass the model so that the resampler can check for validity of
        # newly placed particles.
        # FIXME This feels fishy. If we update particles elsewwhere
        new_distribution = self.resampler(self.model, self)
        self.particle_weights = new_distribution.particle_weights
        self.particle_locations = new_distribution.particle_locations

        # Possibly canonicalize, if we've been asked to do so.
        if self._canonicalize:
            self.particle_locations[:, :] = self.model.canonicalize(self.particle_locations)

        # Instruct the model to clear its cache, demoting any errors to
        # warnings.
        try:
            self.model.clear_cache()
        except Exception as e:
            warnings.warn("Exception raised when clearing model cache: {}. Ignoring.".format(e))

        # Possibly track the new divergence.
        if self._resampling_divergences is not None:
            self._resampling_divergences.append(
                self._kl_divergence(old_locs, old_weights)
            )

        # Report current and previous mean, cov.
        if self._debug_resampling:
            new_mean = self.est_mean()
            new_cov = self.est_covariance_mtx()
            logger.debug("Resampling changed mean by {}. Norm change in cov: {}.".format(
                old_mean - new_mean,
                np.linalg.norm(new_cov - old_cov)
            ))

    def bayes_risk(self, expparams):
        r"""
        Calculates the Bayes risk for hypothetical experiments, assuming the
        quadratic loss function defined by the current model's scale matrix
        (see :attr:`qinfer.abstract_model.Simulatable.Q`).

        :param expparams: The experiments at which to compute the risk.
        :type expparams: :class:`~numpy.ndarray` of dtype given by the current
            model's :attr:`~qinfer.abstract_model.Simulatable.expparams_dtype` property,
            and of shape ``(1,)``

        :return np.ndarray: The Bayes risk for the current posterior distribution
            at each hypothetical experiment in ``expparams``, therefore 
            has shape ``(expparams.size,)``
        """

        # for models whose outcome number changes with experiment, we 
        # take the easy way out and for-loop over experiments
        n_eps = expparams.size
        if n_eps > 1 and not self.model.is_n_outcomes_constant:
            risk = np.empty(n_eps)
            for idx in range(n_eps):
                risk[idx] = self.bayes_risk(expparams[idx, np.newaxis])
            return risk
        
        # outcomes for the first experiment
        os = self.model.domain(expparams[0,np.newaxis])[0].values

        # compute the hypothetical weights, likelihoods and normalizations for
        # every possible outcome 
        w_hyp, L, N = self.hypothetical_update(
                os,
                expparams, 
                return_normalization=True, 
                return_likelihood=True
            )
        # w_hyp.shape == (n_out, n_eps, n_particles)
        # make N.shape == (n_out, n_eps)
        N = N[:,:,0]

        # compute the hypothetical means and variances given outcomes and exparams
        # mu_hyp.shape == (n_out, n_eps, n_models)
        # var_hyp.shape == (n_out, n_eps)
        mu_hyp = np.dot(w_hyp, self.particle_locations)
        var_hyp = np.sum(
            w_hyp * 
            np.sum(self.model.Q * (
                self.particle_locations[np.newaxis,np.newaxis,:,:] - 
                mu_hyp[:,:,np.newaxis,:]
            ) ** 2,  axis=3),
            axis=2
        )
        self._dvar_hyp = var_hyp
        self._dN = N
        # the risk of a given expparam can be calculated as the mean posterior
        # variance weighted over all possible outcomes
        return np.sum(N * var_hyp, axis=0)

    def expected_information_gain(self, expparams):
        r"""
        Calculates the expected information gain for each hypothetical experiment.

        :param expparams: The experiments at which to compute expected
            information gain.
        :type expparams: :class:`~numpy.ndarray` of dtype given by the current
            model's :attr:`~qinfer.abstract_model.Simulatable.expparams_dtype` property,
            and of shape ``(n,)``

        :return float: The expected information gain for each 
            hypothetical experiment in ``expparams``.
        """
        # This is a special case of the KL divergence estimator (see below),
        # in which the other distribution is guaranteed to share support.
        
        # for models whose outcome number changes with experiment, we 
        # take the easy way out and for-loop over experiments
        n_eps = expparams.size
        if n_eps > 1 and not self.model.is_n_outcomes_constant:
            risk = np.empty(n_eps)
            for idx in range(n_eps):
                risk[idx] = self.expected_information_gain(expparams[idx, np.newaxis])
            return risk
        
        # number of outcomes for the first experiment
        os = self.model.domain(expparams[0,np.newaxis])[0].values

        # compute the hypothetical weights, likelihoods and normalizations for
        # every possible outcome and expparam
        # the likelihood over outcomes should sum to 1, so don't compute for last outcome
        w_hyp, L, N = self.hypothetical_update(
                os[:-1], 
                expparams, 
                return_normalization=True, 
                return_likelihood=True
            )
        w_hyp_last_outcome = (1 - L.sum(axis=0)) * self.particle_weights[np.newaxis, :]
        N = np.concatenate([N[:,:,0], np.sum(w_hyp_last_outcome[np.newaxis,:,:], axis=2)], axis=0)
        w_hyp_last_outcome = w_hyp_last_outcome / N[-1,:,np.newaxis]
        w_hyp = np.concatenate([w_hyp, w_hyp_last_outcome[np.newaxis,:,:]], axis=0)
        # w_hyp.shape == (n_out, n_eps, n_particles)
        # N.shape == (n_out, n_eps)

        # compute the Kullback-Liebler divergence for every experiment and possible outcome
        # KLD.shape == (n_out, n_eps)
        KLD = np.sum(w_hyp * np.log(w_hyp / self.particle_weights), axis=2)

        # return the expected KLD (ie expected info gain) for every experiment
        return np.sum(N * KLD, axis=0)

    ## MISC METHODS ###########################################################

    def risk(self, x0):
        return self.bayes_risk(np.array([(x0,)], dtype=self.model.expparams_dtype))

    ## PLOTTING METHODS #######################################################

    def posterior_marginal(self, idx_param=0, res=100, smoothing=0, range_min=None, range_max=None):
        """
        Returns an estimate of the marginal distribution of a given model parameter, based on
        taking the derivative of the interpolated cdf.

        :param int idx_param: Index of parameter to be marginalized.
        :param int res1: Resolution of of the axis.
        :param float smoothing: Standard deviation of the Gaussian kernel
            used to smooth; same units as parameter.
        :param float range_min: Minimum range of the output axis.
        :param float range_max: Maximum range of the output axis.

        .. seealso::

            :meth:`SMCUpdater.plot_posterior_marginal`
        """

        # We need to sort the particles to get cumsum to make sense.
        # interp1d would  do it anyways (using argsort, too), so it's not a waste
        s = np.argsort(self.particle_locations[:,idx_param])
        locs = self.particle_locations[s,idx_param]

        # relevant axis discretization
        r_min = np.min(locs) if range_min is None else range_min
        r_max = np.max(locs) if range_max is None else range_max
        ps = np.linspace(r_min, r_max, res)

        # interpolate the cdf of the marginal distribution using cumsum
        interp = scipy.interpolate.interp1d(
            np.append(locs, r_max + np.abs(r_max-r_min)),
            np.append(np.cumsum(self.particle_weights[s]), 1),
            #kind='cubic',
            bounds_error=False,
            fill_value=0,
            assume_sorted=True
        )

        # get distribution from derivative of cdf, and smooth it
        pr = np.gradient(interp(ps), ps[1]-ps[0])
        if smoothing > 0:
            gaussian_filter1d(pr, res*smoothing/(np.abs(r_max-r_min)), output=pr)

        del interp

        return ps, pr

    def plot_posterior_marginal(self, idx_param=0, res=100, smoothing=0,
            range_min=None, range_max=None, label_xaxis=True,
            other_plot_args={}, true_model=None
        ):
        """
        Plots a marginal of the requested parameter.

        :param int idx_param: Index of parameter to be marginalized.
        :param int res1: Resolution of of the axis.
        :param float smoothing: Standard deviation of the Gaussian kernel
            used to smooth; same units as parameter.
        :param float range_min: Minimum range of the output axis.
        :param float range_max: Maximum range of the output axis.
        :param bool label_xaxis: Labels the :math:`x`-axis with the model parameter name
            given by this updater's model.
        :param dict other_plot_args: Keyword arguments to be passed to
            matplotlib's ``plot`` function.
        :param np.ndarray true_model: Plots a given model parameter vector
            as the "true" model for comparison.

        .. seealso::

            :meth:`SMCUpdater.posterior_marginal`
        """
        res = plt.plot(*self.posterior_marginal(
            idx_param, res, smoothing,
            range_min, range_max
        ), **other_plot_args)
        if label_xaxis:
            plt.xlabel('${}$'.format(self.model.modelparam_names[idx_param]))
        if true_model is not None:
            true_model = true_model[0, idx_param] if true_model.ndim == 2 else true_model[idx_param]
            old_ylim = plt.ylim()
            plt.vlines(true_model, old_ylim[0] - 0.1, old_ylim[1] + 0.1, color='k', linestyles='--')
            plt.ylim(old_ylim)

        return res

    def plot_covariance(self, corr=False, param_slice=None, tick_labels=None, tick_params=None):
        """
        Plots the covariance matrix of the posterior as a Hinton diagram.

        .. note::

            This function requires that mpltools is installed.

        :param bool corr: If `True`, the covariance matrix is first normalized
            by the outer product of the square root diagonal of the covariance matrix
            such that the correlation matrix is plotted instead.
        :param slice param_slice: Slice of the modelparameters to
            be plotted.
        :param list tick_labels: List of tick labels for each component;
            by default, these are drawn from the model itself.
        """
        if mpls is None:
            raise ImportError("Hinton diagrams require mpltools.")

        if param_slice is None:
            param_slice = np.s_[:]

        tick_labels = (
            list(range(len(self.model.modelparam_names[param_slice]))),
            tick_labels
            if tick_labels is not None else
            list(map(u"${}$".format, self.model.modelparam_names[param_slice]))
        )

        cov = self.est_covariance_mtx(corr=corr)[param_slice, param_slice]

        retval = mpls.hinton(cov)
        plt.xticks(*tick_labels, **(tick_params if tick_params is not None else {}))
        plt.yticks(*tick_labels, **(tick_params if tick_params is not None else {}))
        plt.gca().xaxis.tick_top()

        return retval


    def posterior_mesh(self, idx_param1=0, idx_param2=1, res1=100, res2=100, smoothing=0.01):
        """
        Returns a mesh, useful for plotting, of kernel density estimation
        of a 2D projection of the current posterior distribution.

        :param int idx_param1: Parameter to be treated as :math:`x` when
            plotting.
        :param int idx_param2: Parameter to be treated as :math:`y` when
            plotting.
        :param int res1: Resolution along the :math:`x` direction.
        :param int res2: Resolution along the :math:`y` direction.
        :param float smoothing: Standard deviation of the Gaussian kernel
            used to smooth the particle approximation to the current posterior.

        .. seealso::

            :meth:`SMCUpdater.plot_posterior_contour`
        """

        # WARNING: fancy indexing is used here, which means that a copy is
        #          made.
        locs = self.particle_locations[:, [idx_param1, idx_param2]]

        p1s, p2s = np.meshgrid(
            np.linspace(np.min(locs[:, 0]), np.max(locs[:, 0]), res1),
            np.linspace(np.min(locs[:, 1]), np.max(locs[:, 1]), res2)
        )
        plot_locs = np.array([p1s, p2s]).T.reshape((np.prod(p1s.shape), 2))

        pr = np.sum( # <- sum over the particles in the SMC approximation.
            np.prod( # <- product over model parameters to get a multinormal
                # Evaluate the PDF at the plotting locations, with a normal
                # located at the particle locations.
                scipy.stats.norm.pdf(
                    plot_locs[:, np.newaxis, :],
                    scale=smoothing,
                    loc=locs
                ),
                axis=-1
            ) * self.particle_weights,
            axis=1
        ).reshape(p1s.shape) # Finally, reshape back into the same shape as the mesh.

        return p1s, p2s, pr

    def plot_posterior_contour(self, idx_param1=0, idx_param2=1, res1=100, res2=100, smoothing=0.01):
        """
        Plots a contour of the kernel density estimation
        of a 2D projection of the current posterior distribution.

        :param int idx_param1: Parameter to be treated as :math:`x` when
            plotting.
        :param int idx_param2: Parameter to be treated as :math:`y` when
            plotting.
        :param int res1: Resolution along the :math:`x` direction.
        :param int res2: Resolution along the :math:`y` direction.
        :param float smoothing: Standard deviation of the Gaussian kernel
            used to smooth the particle approximation to the current posterior.

        .. seealso::

            :meth:`SMCUpdater.posterior_mesh`
        """
        return plt.contour(*self.posterior_mesh(idx_param1, idx_param2, res1, res2, smoothing))

    ## IPYTHON SUPPORT METHODS ################################################

    def _repr_html_(self):
        return r"""
        <strong>{cls_name}</strong> for model of type <strong>{model}</strong>:
        <table>
            <caption>Current estimated parameters</caption>
            <thead>
                <tr>
                    {parameter_names}
                </tr>
            </thead>
            <tbody>
                <tr>
                    {parameter_values}
                </tr>
            </tbody>
        </table>
        <em>Resample count:</em> {resample_count}
        """.format(
            cls_name=type(self).__name__, # Useful for subclassing.
            model=(
                type(self.model).__name__
                if not self.model.model_chain else
                # FIXME: the <strong> here is ugly as sin.
                "{}</strong> (based on <strong>{}</strong>)<strong>".format(
                    type(self.model).__name__,
                    "</strong>, <strong>".join(type(model).__name__ for model in self.model.model_chain)
                )
            ),

            parameter_names="\n".join(
                map("<td>${}$</td>".format, self.model.modelparam_names)
            ),

            # TODO: change format string based on number of digits of precision
            #       admitted by the variance.
            parameter_values="\n".join(
                "<td>${}$</td>".format(
                    format_uncertainty(mu, std)
                )
                for mu, std in
                zip(self.est_mean(), np.sqrt(np.diag(self.est_covariance_mtx())))
            ),

            resample_count=self.resample_count
        )


class MixedApproximateSMCUpdater(SMCUpdater):
    """
    Subclass of :class:`SMCUpdater` that uses a mixture of two models, one
    of which is assumed to be expensive to compute, while the other is assumed
    to be cheaper. This allows for approximate computation to be used on the
    lower-weight particles.

    :param ~qinfer.abstract_model.Model good_model: The more expensive, but
        complete model.
    :param ~qinfer.abstract_model.Model approximate_model: The less expensive,
        but approximate model.
    :param float mixture_ratio: The ratio of the posterior weight that will
        be delegated to the good model in each update step.
    :param float mixture_thresh: Any particles of weight at least equal to this
        threshold will be delegated to the complete model, irrespective
        of the value of ``mixture_ratio``.
    :param int min_good: Minimum number of "good" particles to assign at each
        step.

    All other parameters are as described in the documentation of
    :class:`SMCUpdater`.
    """

    def __init__(self,
            good_model, approximate_model,
            n_particles, prior,
            resample_a=None, resampler=None, resample_thresh=0.5,
            mixture_ratio=0.5, mixture_thresh=1.0, min_good=0
            ):

        self._good_model = good_model
        self._apx_model = approximate_model

        super(MixedApproximateSMCUpdater, self).__init__(
            good_model, n_particles, prior,
            resample_a, resampler, resample_thresh
        )

        self._mixture_ratio = mixture_ratio
        self._mixture_thresh = mixture_thresh
        self._min_good = min_good

    def hypothetical_update(self, outcomes, expparams, return_likelihood=False, return_normalization=False):
        # TODO: consolidate code with SMCUpdater by breaking update logic
        #       into private method.

        # It's "hypothetical", don't want to overwrite old weights yet!
        weights = self.particle_weights
        locs = self.particle_locations

        # Check if we have a single outcome or an array. If we only have one
        # outcome, wrap it in a one-index array.
        if not isinstance(outcomes, np.ndarray):
            outcomes = np.array([outcomes])

        # Make an empty array for likelihoods. We'll fill it in in two steps,
        # the good step and the approximate step.
        L = np.zeros((outcomes.shape[0], locs.shape[0], expparams.shape[0]))

        # Which indices go to good_model?

        # Start by getting a permutation that sorts the weights.
        # Since sorting as implemented by NumPy is stable, we want to break
        # that stability to avoid introducing patterns, and so we first
        # randomly shuffle the identity permutation.
        idxs_random = np.arange(weights.shape[0])
        np.random.shuffle(idxs_random)
        idxs_sorted = np.argsort(weights[idxs_random])

        # Find the inverse permutation to be that which returns
        # the composed permutation sort º shuffle to the identity.
        inv_idxs_sort = np.argsort(idxs_random[idxs_sorted])

        # Now strip off a set of particles producing the desired total weight
        # or that have weights above a given threshold.
        sorted_weights = weights[idxs_random[idxs_sorted]]
        cum_weights = np.cumsum(sorted_weights)
        good_mask = (np.logical_or(
            cum_weights >= 1 - self._mixture_ratio,
            sorted_weights >= self._mixture_thresh
        ))[inv_idxs_sort]
        if np.sum(good_mask) < self._min_good:
            # Just take the last _min_good instead of something sophisticated.
            good_mask = np.zeros_like(good_mask)
            good_mask[idxs_random[idxs_sorted][-self._min_good:]] = True
        bad_mask = np.logical_not(good_mask)

        # Finally, separate out the locations that go to each of the good and
        # bad models.
        locs_good = locs[good_mask, :]
        locs_bad = locs[bad_mask, :]

        assert_thresh=1e-6
        assert np.mean(weights[good_mask]) - np.mean(weights[bad_mask]) >= -assert_thresh

        # Now find L for each of the good and bad models.
        L[:, good_mask, :] = self._good_model.likelihood(outcomes, locs_good, expparams)
        L[:, bad_mask, :] = self._apx_model.likelihood(outcomes, locs_bad, expparams)
        L = L.transpose([0, 2, 1])

        # update the weights sans normalization
        # Rearrange so that likelihoods have shape (outcomes, experiments, models).
        # This makes the multiplication with weights (shape (models,)) make sense,
        # since NumPy broadcasting rules align on the right-most index.
        hyp_weights = weights * L

        # Sum up the weights to find the renormalization scale.
        norm_scale = np.sum(hyp_weights, axis=2)[..., np.newaxis]

        # As a special case, check whether any entries of the norm_scale
        # are zero. If this happens, that implies that all of the weights are
        # zero--- that is, that the hypothicized outcome was impossible.
        # Conditioned on an impossible outcome, all of the weights should be
        # zero. To allow this to happen without causing a NaN to propagate,
        # we forcibly set the norm_scale to 1, so that the weights will
        # all remain zero.
        #
        # We don't actually want to propagate this out to the caller, however,
        # and so we save the "fixed" norm_scale to a new array.
        fixed_norm_scale = norm_scale.copy()
        fixed_norm_scale[np.abs(norm_scale) < np.spacing(1)] = 1

        # normalize
        norm_weights = hyp_weights / fixed_norm_scale
            # Note that newaxis is needed to align the two matrices.
            # This introduces a length-1 axis for the particle number,
            # so that the normalization is broadcast over all particles.
        if not return_likelihood:
            if not return_normalization:
                return norm_weights
            else:
                return norm_weights, norm_scale
        else:
            if not return_normalization:
                return norm_weights, L
            else:
                return norm_weights, L, norm_scale

class SMCUpdaterBCRB(SMCUpdater):
    """

    Subclass of :class:`SMCUpdater`, adding Bayesian Cramer-Rao bound
    functionality.

    Models considered by this class must be differentiable.

    In addition to the arguments taken by :class:`SMCUpdater`, this class
    takes the following keyword-only arguments:

    :param bool adaptive: If `True`, the updater will track both the
        non-adaptive and adaptive Bayes Information matrices.
    :param initial_bim: If the regularity conditions are not met, then taking
        the outer products of gradients over the prior will not give the correct
        initial BIM. In such cases, ``initial_bim`` can be set to the correct
        BIM corresponding to having done no experiments.
    """



    def __init__(self, *args, **kwargs):
        SMCUpdater.__init__(self, *args, **{
            key: kwargs[key] for key in kwargs
            if key in [
                'resampler_a', 'resampler', 'resample_thresh', 'model',
                'prior', 'n_particles'
            ]
        })

        if not isinstance(self.model, DifferentiableModel):
            raise ValueError("Model must be differentiable.")

        # TODO: fix distributions to make grad_log_pdf return the right
        #       shape, such that the indices are
        #       [idx_model, idx_param] → [idx_model, idx_param],
        #       so that prior.grad_log_pdf(modelparams[i, j])[i, k]
        #       returns the partial derivative with respect to the kth
        #       parameter evaluated at the model parameter vector
        #       modelparams[i, :].
        if 'initial_bim' not in kwargs or kwargs['initial_bim'] is None:
            gradients = self.prior.grad_log_pdf(self.particle_locations)
            self._current_bim = np.sum(
                gradients[:, :, np.newaxis] * gradients[:, np.newaxis, :],
                axis=0
            ) / self.n_particles
        else:
            self._current_bim = kwargs['initial_bim']

        # Also track the adaptive BIM, if we've been asked to.
        if "adaptive" in kwargs and kwargs["adaptive"]:
            self._track_adaptive = True
            # Both the prior- and posterior-averaged BIMs start
            # from the prior.
            self._adaptive_bim = self.current_bim
        else:
            self._track_adaptive = False

    # TODO: since we are guaranteed differentiability, and since SMCUpdater is
    #       now a Distribution subclass representing posterior sampling, write
    #       a grad_log_pdf for the posterior distribution, perhaps?

    def _bim(self, modelparams, expparams, modelweights=None):
        # TODO: document
        #       rough idea of this function is to take expectations of an
        #       FI over some distribution, here represented by modelparams.

        # NOTE: The signature of this function is a bit odd, but it allows
        #       us to represent in one function both prior samples of uniform
        #       weight and weighted samples from a posterior.
        #       Because it's a bit odd, we make it a private method and expose
        #       functionality via the prior_bayes_information and
        #       posterior_bayes_information methods.

        # About shapes: the FI we will be averaging over has four indices:
        # FI[i, j, m, e], i and j being matrix indices, m being a model index
        # and e being a model index.
        # We will thus want to return an array of shape BI[i, j, e], reducing
        # over the model index.
        fi = self.model.fisher_information(modelparams, expparams)

        # We now either reweight and sum, or sum and divide, based on whether we
        # have model weights to consider or not.
        if modelweights is None:
            # Assume uniform weights.
            bim = np.sum(fi, axis=2) / modelparams.shape[0]
        else:
            bim = np.einsum("m,ijme->ije", modelweights, fi)

        return bim


    @property
    def current_bim(self):
        """
        Returns a copy of the current Bayesian Information Matrix (BIM)
        of the :class:`SMCUpdaterBCRB`

        :returns: `np.array` of shape [idx_modelparams,idx_modelparams]
        """
        return np.copy(self._current_bim)

    @property
    def adaptive_bim(self):
        """
        Returns a copy of the adaptive Bayesian Information Matrix (BIM)
        of the :class:`SMCUpdaterBCRB`. Will raise an error if
        `method`:`SMCUpdaterBCRB.track_adaptive` is `False`.

        :returns: `np.array` of shape [idx_modelparams,idx_modelparams]
        """
        if not self.track_adaptive:
            raise ValueError('To track the adaptive_bim, the adaptive keyword argument'
                'must be set True when initializing class.')
        return np.copy(self._adaptive_bim)

    @property
    def track_adaptive(self):
        """
        If `True` the :class:`SMCUpdaterBCRB` will track the adaptive BIM. Set by
        keyword argument `adaptive` to :method:`SMCUpdaterBCRB.__init__`.

        :returns: `bool`
        """
        return self._track_adaptive




    def prior_bayes_information(self, expparams, n_samples=None):
        """
        Evaluates the local Bayesian Information Matrix (BIM) for a set of
        samples from the SMC particle set, with uniform weights.

        :param expparams: Parameters describing the experiment that was
            performed.
        :type expparams: :class:`~numpy.ndarray` of dtype given by the
            :attr:`~qinfer.abstract_model.Model.expparams_dtype` property
            of the underlying model

        :param n_samples int: Number of samples to draw from particle distribution,
                        to evaluate BIM over.
        """

        if n_samples is None:
            n_samples = self.particle_locations.shape[0]
        return self._bim(self.prior.sample(n_samples), expparams)

    def posterior_bayes_information(self, expparams):
        """
        Evaluates the local Bayesian Information Matrix (BIM) over all particles
        of the current posterior distribution with corresponding weights.

        :param expparams: Parameters describing the experiment that was
            performed.
        :type expparams: :class:`~numpy.ndarray` of dtype given by the
            :attr:`~qinfer.abstract_model.Model.expparams_dtype` property
            of the underlying model

        """
        return self._bim(
            self.particle_locations, expparams,
            modelweights=self.particle_weights
        )

    def update(self, outcome, expparams,check_for_resample=True):
        """
        Given an experiment and an outcome of that experiment, updates the
        posterior distribution to reflect knowledge of that experiment.

        After updating, resamples the posterior distribution if necessary.

        :param int outcome: Label for the outcome that was observed, as defined
            by the :class:`~qinfer.abstract_model.Model` instance under study.
        :param expparams: Parameters describing the experiment that was
            performed.
        :type expparams: :class:`~numpy.ndarray` of dtype given by the
            :attr:`~qinfer.abstract_model.Model.expparams_dtype` property
            of the underlying model
        :param bool check_for_resample: If :obj:`True`, after performing the
            update, the effective sample size condition will be checked and
            a resampling step may be performed.
        """
        # Before we update, we need to commit the new Bayesian information
        # matrix corresponding to the measurement we just made.
        self._current_bim += self.prior_bayes_information(expparams)[:, :, 0]

        # If we're tracking the information content accessible to adaptive
        # algorithms, then we must use the current posterior as the prior
        # for the next step, then add that accordingly.
        if self._track_adaptive:
            self._adaptive_bim += self.posterior_bayes_information(expparams)[:, :, 0]

        # We now can update as normal.
        SMCUpdater.update(self, outcome, expparams,check_for_resample=check_for_resample)
