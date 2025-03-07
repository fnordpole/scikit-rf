from __future__ import annotations

import logging
import os
import warnings
from timeit import default_timer as timer
from typing import TYPE_CHECKING, Any

import numpy as np
from scipy import integrate
from scipy.signal import find_peaks
from scipy.linalg import issymmetric
from scipy.optimize import minimize
import matplotlib.pyplot as mplt

from .util import Axes, axes_kwarg

# imports for type hinting
if TYPE_CHECKING:
    from .network import Network


logger = logging.getLogger(__name__)


class VectorFitting:
    """
    This class provides a Python implementation of the Vector Fitting algorithm and various functions for the fit
    analysis, passivity evaluation and enforcement, and export of SPICE equivalent circuits.

    Parameters
    ----------
    network : :class:`skrf.network.Network`
            Network instance of the :math:`N`-port holding the frequency responses to be fitted, for example a
            scattering, impedance or admittance matrix.

    Examples
    --------
    Load the `Network`, create a `VectorFitting` instance, perform the fit with a given number of real and
    complex-conjugate starting poles:

    >>> nw_3port = skrf.Network('my3port.s3p')
    >>> vf = skrf.VectorFitting(nw_3port)
    >>> vf.vector_fit(n_poles_real=1, n_poles_cmplx=4)

    Notes
    -----
    The fitting code is based on the original algorithm [#Gustavsen_vectfit]_ and on two improvements for relaxed pole
    relocation [#Gustavsen_relaxed]_ and efficient (fast) solving [#Deschrijver_fast]_. See also the Vector Fitting
    website [#vectfit_website]_ for further information and download of the papers listed below. A Matlab implementation
    is also available there for reference.

    References
    ----------
    .. [#Gustavsen_vectfit] B. Gustavsen, A. Semlyen, "Rational Approximation of Frequency Domain Responses by Vector
        Fitting", IEEE Transactions on Power Delivery, vol. 14, no. 3, pp. 1052-1061, July 1999,
        DOI: https://doi.org/10.1109/61.772353

    .. [#Gustavsen_relaxed] B. Gustavsen, "Improving the Pole Relocating Properties of Vector Fitting", IEEE
        Transactions on Power Delivery, vol. 21, no. 3, pp. 1587-1592, July 2006,
        DOI: https://doi.org/10.1109/TPWRD.2005.860281

    .. [#Deschrijver_fast] D. Deschrijver, M. Mrozowski, T. Dhaene, D. De Zutter, "Marcomodeling of Multiport Systems
        Using a Fast Implementation of the Vector Fitting Method", IEEE Microwave and Wireless Components Letters,
        vol. 18, no. 6, pp. 383-385, June 2008, DOI: https://doi.org/10.1109/LMWC.2008.922585

    .. [#vectfit_website] Vector Fitting website: https://www.sintef.no/projectweb/vectorfitting/
    """

    def __init__(self, network: Network):
        self.network = network
        """ Instance variable holding the Network to be fitted. This is the Network passed during initialization,
        which may be changed or set to *None*. """

        self.poles = None
        """ Instance variable holding the list of fitted poles. Will be initialized by :func:`vector_fit`. """

        self.residues = None
        """ Instance variable holding the list of fitted residues. Will be initialized by :func:`vector_fit`. """

        self.proportional = None
        """ Instance variable holding the list of fitted proportional coefficients. Will be initialized by
        :func:`vector_fit`. """

        self.constant = None
        """ Instance variable holding the list of fitted constants. Will be initialized by :func:`vector_fit`. """

        self.map_idx_response_to_idx_pole_group = None
        """ Instance variable holding a map that maps the idx_response to idx_pole_group
        Will be initialized by :func:`vector_fit`. """

        self.map_idx_response_to_idx_pole_group_member = None
        """ Instance variable holding a map that maps idx_response to idx_pole_group_member
        Will be initialized by :func:`vector_fit`. """

        self.d_tilde_history = []
        self.delta_rel_max_singular_value_A_dense_history = []
        self.history_max_sigma = []
        self.history_cond_A_dense = []
        self.history_rank_deficiency_A_dense = []

    @staticmethod
    def get_spurious(poles: np.ndarray, residues: np.ndarray, spurious_pole_threshold: float = 0.03) -> np.ndarray:
        """
        Classifies fitted pole-residue pairs as spurious or not spurious. The implementation is based on the evaluation
        of band-limited energy norms (p=2) of the resonance curves of individual pole-residue pairs, as proposed in
        [#Grivet-Talocia]_.

        Parameters
        ----------
        poles : ndarray, shape (N)
            Array of fitted poles

        residues : ndarray, shape (M, N)
            Array of fitted residues

        spurious_pole_threshold : float, optional
            Sensitivity threshold for the classification. Typical values range from 0.01 to 0.05.

        Returns
        -------
        ndarray, bool, shape (M)
            Boolean array having the same shape as :attr:`poles`. `True` marks the respective pole as spurious.

        References
        ----------
        .. [#Grivet-Talocia] S. Grivet-Talocia and M. Bandinu, "Improving the convergence of vector fitting for
            equivalent circuit extraction from noisy frequency responses," in IEEE Transactions on Electromagnetic
            Compatibility, vol. 48, no. 1, pp. 104-120, Feb. 2006, DOI: https://doi.org/10.1109/TEMC.2006.870814
        """

        omega_poles_min=np.min(poles.imag) # This can be zero if we have no complex poles
        omega_poles_max=np.max(poles.imag) # This can be zero if we have no complex poles

        # Only complex poles are considered
        indices_poles_complex = np.nonzero(poles.imag != 0)[0]
        indices_poles_real = np.nonzero(poles.imag == 0)[0]

        # Alle residues are considered
        n_responses=np.size(residues, axis=0)

        # Initialize to False for every pole
        spurious=np.repeat(False, len(poles))

        # Immediately return if we have no complex poles
        if len(indices_poles_complex) == 0:
            return spurious

        # Define function for integration
        def H_complex(omega, residue, pole):
            s = 1j * omega
            return np.abs(residue / (s - pole) + np.conj(residue) / (s - np.conj(pole)))**2

        def H_real(omega, residue, pole):
            s = 1j * omega
            return np.abs(residue / (s - pole) )**2

        # Collects all norms
        norm2_complex = np.zeros((n_responses, len(indices_poles_complex)))
        norm2_real = np.zeros((n_responses, len(indices_poles_real)))
        norm2_all = np.zeros((n_responses, np.size(poles, axis = 0)))

        integrate_from = omega_poles_min / 3
        integrate_to = omega_poles_max * 3
        for idx_response in range(n_responses):
            idx_pole_complex = 0
            idx_pole_real = 0
            for idx_pole, pole in enumerate(poles):

                if np.imag(pole) == 0:
                    # Real pole
                    integrate_from = np.abs(pole) / 10
                    integrate_to = np.abs(pole) * 10
                    y, err = integrate.quad(H_real, integrate_from, integrate_to,
                                            args=(residues[idx_response, idx_pole], pole))
                    norm2_all[idx_response, idx_pole] = np.sqrt(y)
                    norm2_real[idx_response, idx_pole_real] = np.sqrt(y)
                    idx_pole_real += 1
                    # Debug:
                    # Plot what has been integrated for debug
                    # import matplotlib.pyplot as plt
                    # omega_eval = np.linspace(integrate_from, integrate_to, 200)
                    # fig, ax = plt.subplots()
                    # ax.grid()
                    # ax.plot(omega_eval, [H_real(o, residues[idx_response, idx_pole], poles[idx_pole]) for o in omega_eval], linewidth=2.0)
                    # plt.show()
                else:
                    # Imag pole
                    integrate_from = np.abs(np.imag(pole)) / 10
                    integrate_to = np.abs(np.imag(pole)) * 10
                    y, err = integrate.quad(H_complex, integrate_from, integrate_to,
                                            args=(residues[idx_response, idx_pole], pole))
                    norm2_all[idx_response, idx_pole] = np.sqrt(y)

                    norm2_complex[idx_response, idx_pole_complex] = np.sqrt(y)
                    idx_pole_complex += 1

                    # Debug:
                    # Plot what has been integrated for debug
                    # import matplotlib.pyplot as plt
                    # omega_eval = np.linspace(integrate_from, integrate_to, 200)
                    # fig, ax = plt.subplots()
                    # ax.grid()
                    # ax.plot(omega_eval, [H_real(o, residues[idx_response, idx_pole], poles[idx_pole]) for o in omega_eval], linewidth=2.0)
                    # plt.show()

        # Calculate mean norm of all pole residue terms
        norm2_mean = np.mean(norm2_all)
        norm2_complex_mean = np.mean(norm2_complex)

        # Set spurios flag if norm of complex pole residue term is contributing less than threshold to mean norm
        # TODO: Is the reference the norm2_mean or norm2_complex_mean?
        spurious[indices_poles_complex] = np.all((norm2_complex / norm2_mean) < spurious_pole_threshold, axis=0)

        return spurious

    def get_model_order(self, idx_pole_group = None) -> int:
        """
        Returns the model order calculated with :math:`N_{real} + 2 N_{complex}` for a given set of poles.

        Parameters
        ----------
        idx_pole_group: ndarray with pole groups to be considered, optional
            If not specified, the overall model order will be returned

        Returns
        -------
        order: int
        """

        if idx_pole_group is None:
            pole_group_indices = np.arange(len(self.poles))
        else:
            pole_group_indices = np.array([idx_pole_group])

        model_order = 0
        for idx_pole_group in pole_group_indices:
            model_order += np.sum((self.poles[idx_pole_group].imag != 0) + 1)

        return model_order

    def _check_and_enforce_data_passivity_at_dc(self,
        preserve_dc,
        enforce_data_passivity_at_dc = True,
        enforce_data_real_at_dc = True,
        extrapolate_to_dc = True,
        method = 'svd',
        ):
        # Enforces the dc point in the data to be passive using one of two methods.
        #
        # Method 'optimizer' uses a cost function that is minimized. It minimizes the deviation
        # in the S_DC and the maximum singular value.
        #
        # Method 'svd' (default) uses singular value decomposition based clippig.

        n_ports = int(np.sqrt(np.size(self.responses, axis = 0)))

        # Check whether data has dc
        if not self.omega[0] == 0:
            if extrapolate_to_dc:
                warnings.warn('Warning: Data has no DC point. Extrapolating to DC. Model will be inaccurate at DC',
                              UserWarning, stacklevel=2)
                # Interpolating real and imaginary parts separately
                S1 = self.network.s[0]
                S2 = self.network.s[1]
                omega1 = self.omega[0]
                omega2 = self.omega[1]
                S_DC_real = S1.real + (0 - omega1) * (S2.real - S1.real) / (omega2 - omega1)

                # Update responses and omega with DC point
                responses_new = np.empty(
                    (np.size(self.responses, axis = 0), np.size(self.responses, axis = 1) + 1), dtype=complex)

                for i in range(n_ports):
                    for j in range(n_ports):
                        idx_response = i * n_ports + j

                        responses_new[idx_response, 0] = S_DC_real[i, j]
                        responses_new[idx_response, 1:] = self.responses[idx_response, :]

                self.responses = responses_new
                self.omega = np.insert(self.omega, 0, 0)
            else:
                if preserve_dc:
                    raise RuntimeError('Error: Data has no DC point and extrapolation to DC is disabled but'
                                       'a DC point is required when preserve_dc is used.')

                warnings.warn('Warning: Data has no DC point. Model will be inaccurate at DC',
                              UserWarning, stacklevel=2)
                return
        else:
            # Get S_DC from network
            S_DC_real = np.real(self.network.s[0])
            S_DC_imag = np.imag(self.network.s[0])

            # Warn if we have a large imaginary part at DC
            dc_imag_threshold = 1e-12
            for i in range(n_ports):
                for j in range(n_ports):
                    if np.abs(S_DC_imag[i, j]) > dc_imag_threshold:
                       warnings.warn(f'Warning: Data DC point has a large imaginary part {S_DC_imag[i, j]} in response '
                                     f'({i}, {j})', UserWarning, stacklevel=2)

            # Enforce data real only at DC
            print('Enforcing real only data at DC')
            if enforce_data_real_at_dc:
                # Update DC point to real-only in responses
                for i in range(n_ports):
                    for j in range(n_ports):
                        idx_response = i * n_ports + j
                        self.responses[idx_response, 0] = S_DC_real[i, j]

        # Get new S_DC
        S_DC = S_DC_real

        # Check if passive
        singular_values = np.linalg.svd(S_DC_real, compute_uv=False)
        max_singular_value = np.max(singular_values)
        is_passive = max_singular_value < 1

        # Return if passive
        if is_passive:
            print('Data is passive at DC')
            return
        else:
            warnings.warn('Warning: Data is not passive at DC.', UserWarning, stacklevel=2)

        # Check whether enforce data passivity at dc is enabled
        if not enforce_data_passivity_at_dc:
            warnings.warn('Warning: Data passivity enforcement at DC is disabled.', UserWarning, stacklevel=2)
            return

        print('Starting data passivity enforcement at DC')

        # Save for comparison post optimization
        S_DC_original = np.copy(S_DC)

        if method == 'optimizer':
            # Define cost function for optimizer
            N = S_DC.shape[0]

            # Weights for cost function terms. Need to set a very strong weight on deviation_term
            # otherwise the S_DC will all end up being zero after optimization
            alpha = 1.0
            beta = 1.0
            def cost_function(S_flat):
                # Calculate fidelity term based on maximum singular value
                S = S_flat.reshape(N, N)
                singular_values = np.linalg.svd(S, compute_uv=False)
                fidelity_term = alpha * (np.max(singular_values) - (1 - 1e-12))
                if fidelity_term < 0:
                    fidelity_term = 0

                # Deviation from the original S matrix
                deviation_term = beta * np.linalg.norm(S - S_DC_original, ord='fro')**2

                return fidelity_term + deviation_term

            S_initial = S_DC.flatten()
            result = minimize(cost_function, S_initial, method='L-BFGS-B')
            S_DC = result.x.reshape(N, N)

        elif method == 'svd':
            # Use SVD based singular value clipping

            # Singular value decomposition
            u, sigma, vh = np.linalg.svd(S_DC, full_matrices=False)

            # Set all sigma that are >= 1 to 1
            # Note: Setting to exactly 1 can lead to passivity tests failing because of precision problems.
            # Thus, I subtract 1 - 1e-12 instead
            sigma[sigma >= 1] = 1 - 1e-12

            # Calculate new S_DC
            S_DC = (u * sigma) @ vh

        # Post-passsivity enforcement passivity check
        singular_values = np.linalg.svd(S_DC, compute_uv=False)
        max_singular_value = np.max(singular_values)
        is_passive = max_singular_value <= 1

        # Calculate dS/S
        S_DC_delta = S_DC - S_DC_original
        S_DC_delta_norm_rel = \
            np.linalg.norm(S_DC_delta, ord='fro') / np.linalg.norm(S_DC_original, ord='fro')
        logger.info(f'Data passivity enforcement dS/S = {S_DC_delta_norm_rel:.3e}')

        if not is_passive:
            warnings.warn('Warning: Data passivity enforcement at DC failed', UserWarning, stacklevel=2)
            return

        print('Data passivity enforcement at DC succeeded')

        # Update DC point in responses
        for i in range(n_ports):
            for j in range(n_ports):
                idx_response = i * n_ports + j
                self.responses[idx_response, 0] = S_DC[i, j]

    def _print_algorithm_info_messsage(self, preserve_dc, fit_constant, fit_proportional):
        # Warn if fit_constant is enabled while dc_preserving fit is also enabled
        if fit_constant and preserve_dc:
            warnings.warn('Ignoring fit_constant=True because preserve_dc is enabled')

        # Print algorithm info message
        if preserve_dc:
            print(f'Algorithm info: preserve_dc={preserve_dc} fit_proportional={fit_proportional}')
        else:
            print(f'Algorithm info: preserve_dc={preserve_dc} fit_constant={fit_constant} '
                  f'fit_proportional={fit_proportional}')

    def vector_fit(self,
                 # Initial poles
                 poles_init = None,
                 n_poles_init = None, poles_init_type = 'complex', poles_init_spacing = 'lin',

                 # Weighting and fit options
                 weights = None,
                 weighting: str = 'uniform',
                 weighting_accuracy_db: float = -60.0,

                 # Fit constant and/or proportional term
                 fit_constant: bool = True,
                 fit_proportional: bool = False,

                 # Share poles between responses
                 pole_sharing: str = 'MIMO',
                 pole_groups = None,

                 # Parameters for the vector fitting algorithm
                 max_iterations: int = 200,
                 stagnation_threshold: float = 1e-6,
                 abstol: float = 1e-3,

                 # Memory saver
                 memory_saver: bool = False,

                 # Parametertype
                 parameter_type: str = 's',

                 # Verbose
                 verbose = False,

                 # Enforce dc data passivity
                 enforce_data_passivity_at_dc = True,

                 # Enable dc preserving fit
                 preserve_dc = True,
                 ) -> None:
        """
        Main work routine performing the vector fit. The results will be stored in the class variables
        :attr:`poles`, :attr:`residues`, :attr:`proportional` and :attr:`constant`.

        Parameters
        ----------
        poles_init: numpy array of initial poles, or list of numpy arrays of initial poles, optional.
            If specified, those poles will be used as initial poles.
            If specified as a list, the list elements correspond to the pole groups

        n_poles_init: int, or list of int, optional
            Number of poles in the initial model.
            If not specified, the number of initial poles will be estimated from the data
            If specified as a list, the list elements correspond to the pole groups

        poles_init_type: str, or list of str, optional
            Type of poles in the initial model. Can be 'complex' or 'real'. Only used if n_poles_init is specified.
            Otherwise the type of initial poles is estimated from the data.
            If specified as a list, the list elements correspond to the pole groups

        poles_init_spacing: str, or list of str, optional
            Spacing of the initial poles in the frequency range. Only used if n_poles_init is specified.
            Otherwise the poles will be estimated from the data.
            If specified as a list, the list elements correspond to the pole groups

        weights: numpy ndarray of size n_responses, n_freqs, optional
            If weights are provided, these weights are used. The weights must be in the order
            The rows must be in this order: W11, W12, W13,...W21, W22,... where Wij is the weight
            vector used to calculate Sij * Wij.

            Alternatively to providing the weights yourself, you can set thei weighting parameter
            (and accompanying weighting_accuracy_db parameter) to have the weights created from the data.

        weighting: str, optional
            Weighting to be used for the frequency responses. The default is uniform weigthing.

            'uniform': Uniform weighting: Every frequency sample has the same weight. Favors absolute accuracy.
            Advantages: Ensures that no frequency range is prioritized over another.
            Disadvantages: May lead to poor accuracy in regions where the frequency response magnitude is much smaller.

            'inverse_magnitude': Inverse magnitude weighting: Weight is inversely proportional to the magnitude of the
            frequency response. Weight=1/abs(f(s)). Favors relative accuracy. Advantages: Improves the relative accuracy
            in low-magnitude regions, ensuring that small values in the response are not overshadowed by larger ones.
            Disadvantages: May overly emphasize small-magnitude regions, leading to less accuracy in high-magnitude
            regions or increased numerical sensitivity. Can amplify noise if the response magnitude is very small.

        weighting_accuracy_db: float, optional
            In inverse magnitude weighting, specifies a limit in dB (dB20), down to which the magnitudes are weighted.

            The possible range is -inf to 0 dB. If set to 0 dB, the minimum weight is 1, which is effectively the
            same as uniform weighting (all weights 1.0).

            Example: If you set it to -20 dB, all values are weighted according to their inverse magnitude 1/abs(value),
            but with a limit of 0.1: All values less than 0.1 are weighted with 1/0.1 but no less than that.

            In summary, with this parameter the accuracy tradeoff between large and small magnitudes can be adjusted.

            For example, if you want the same accuracy down to -80 dB as for larger values around 0 dB, you can set
            weighting_accuracy_db=-80. This will fit all values down to -80 dB with the same relative accuracy as those
            around 0 dB. Important note: It is inevitable that this will lead to a higher absolute RMS error of the
            entire fit. This is because the fit accuracy for larger values will now be traded off against the fit
            accuracy for small values.

            For this reason it is important not to set weighting_accuracy_db lower than you actually need, because
            otherwise you will sacrifice accuracy on the larger values for extra accuracy on very small values
            that you may not even be interested in. Note that setting it to, for example, -40 dB, also the -80 dB
            range values will benefit from more accuracy, but only to a certain extent limited by the -40 dB
            weighting_accuracy_db.

            The default was chosen to -60 dB because it may fit most practical applications, putting weight on values
            down to -60 dB but also not sacrificing too much accuracy in the 0 dB range.

        fit_constant: bool, optional
            Decide whether the constant term d is fitted or not.
            If preserve_dc is True, fit_constant is ignored and the DC point will always be set to the
            exact value of the data at DC.

        fit_proportional: bool, optional
            Decide whether the proportional term e is fitted or not.

        pole_sharing: bool, optional
            Decide whether to share one common pole set for all responses or
            use separate pole sets for each response.

        max_iterations: int, optional
            Maximum number of iterations for the fitting process.

        abstol: float, optional
            Absolute error threshold. The algorithm stops iterating once the absolute error of the fit is less than
            abstol. Because a residue fit and a delta calculation needs to be done, this check is only done every
            25 iterations.

        stagnation_threshold: float, optional
            The algorithm stops iterating if the relative change of the maximum absolute singular value of the system
            matrix from one iteration to the next is less than stagnation_threshold.

        parameter_type : str, optional
            Representation type of the frequency responses to be fitted. Either *scattering* (`'s'` or `'S'`),
            *impedance* (`'z'` or `'Z'`) or *admittance* (`'y'` or `'Y'`). It's recommended to perform the fit on the
            original S parameters. Otherwise, scikit-rf will convert the responses from S to Z or Y, which might work
            for the fit but can cause other issues.

        memory_saver: bool, optional
            Enables the memory saver. If enabled, the runtime might be longer but the memory usage is reduced.
            Use it for very large data sets if memory is the limiting factor.

        enforce_data_passivity_at_dc: bool, optional
            Enables the enforcement of the passivity of the DC point before fitting. The DC point cannot be modified
            by post-fit passivity enforcement because it needs to be exact. So the DC point of the fit will be just
            set to the DC point of the data. The model thus can only be passive if the DC point is passive as well.

            Circuit simulators numeric errors or measurments can lead to an (even sligthly) non-passive DC point.
            If you enable this setting, the DC point will be perturbed as slightly as possible (optimizer) until
            it is passive. This will lead to an error at DC but it is unavoidable to make the model passive at DC.

            A warning will be printed if the DC point is non-passive in any case, so you have the chance to provide
            better data at DC that is passive, avoiding subsequent errors due to the passivity enforcement.

        preserve_dc: bool, optional
            Enables a DC preserving with with modified rational basis functions. The DC point of the fit will be
            exactly the DC point of the data and the fit will not modify it.

            It is important to set enforce_data_passivity_at_dc to True because the passivity_enforce() algorithm
            will also not be able to modify the DC point if you fitted with preserve_dc = True. Thus, if the
            DC point in the data is not passive, it will be impossible to make the entire model passive.

        Returns
        -------
        None
            No return value.

        Notes
        -----
        The required number of real or complex conjugate starting poles depends on the behaviour of the frequency
        responses. To fit a smooth response such as a low-pass characteristic, 1-3 real poles and no complex conjugate
        poles is usually sufficient. If resonances or other types of peaks are present in some or all of the responses,
        a similar number of complex conjugate poles is required. Be careful not to use too many poles, as excessive
        poles will not only increase the computation workload during the fitting and the subsequent use of the model,
        but they can also introduce unwanted resonances at frequencies well outside the fit interval.

        See Also
        --------
        auto_fit : Automatic vector fitting routine with pole adding and skimming.
        """
        # Start timer to track run time
        timer_start = timer()
        pole_sharing = pole_sharing.lower()

        # Get omega
        self.omega = self._get_omega_from_network()

        # Get responses
        self.responses = self._get_responses(parameter_type)
        n_responses = np.size(self.responses, axis=0)

        # Check and enforce passivity at DC
        self._check_and_enforce_data_passivity_at_dc(preserve_dc, enforce_data_passivity_at_dc)

        # Get weights
        if weights is None:
            self.weights=self._get_weights(weighting, weighting_accuracy_db)

        # Initialize pole sharing
        self._initialize_pole_sharing(pole_sharing, n_responses, pole_groups)
        n_pole_groups = len(self.poles)

        # Convert n_poles_init to list if it is provided as an int
        if not isinstance(n_poles_init, list):
            n_poles_init = [n_poles_init] * n_pole_groups

        # Convert poles_init_type to list if it is provided as a str
        if not isinstance(poles_init_type, list):
            poles_init_type = [poles_init_type] * n_pole_groups

        # Convert poles_init_spacing to list if it is provided as a str
        if not isinstance(poles_init_spacing, list):
            poles_init_spacing = [poles_init_spacing] * n_pole_groups

        # Convert poles_init to list if it is provided as a numpy array
        if poles_init is not None and not isinstance(poles_init, list):
            poles_init = [poles_init] * n_pole_groups

        # Print algorithm info message
        self._print_algorithm_info_messsage(preserve_dc, fit_constant, fit_proportional)

        # Fit each pole group
        for idx_pole_group in range(n_pole_groups):
            # Get the indices of all responses that are part of the pole group
            indices_responses=np.nonzero(self.map_idx_response_to_idx_pole_group == idx_pole_group)

            logger.info(f'Starting vector_fit for pole group {idx_pole_group+1} of {n_pole_groups}')

            if poles_init is None:
                # Get initial poles
                poles = self._get_initial_poles(self.omega, n_poles_init[idx_pole_group], poles_init_type[idx_pole_group],
                                                poles_init_spacing[idx_pole_group], self.responses[indices_responses])
            else:
                # Set initial poles to user-provided poles
                poles = poles_init[idx_pole_group]

            # Call _vector_fit
            poles, residues, constant, proportional = self._vector_fit(
                poles, self.omega, self.responses[indices_responses], self.weights[indices_responses],
                fit_constant, fit_proportional, memory_saver, preserve_dc,
                max_iterations, stagnation_threshold, abstol)

            # Save results
            self._save_results(
                poles, residues, constant, proportional, idx_pole_group)

            logger.info(f'Finished vector_fit for pole group {idx_pole_group+1} of {n_pole_groups}')

        wall_clock_time = timer() - timer_start
        logger.info(f'Finished vector_fit. Time elapsed = {wall_clock_time:.4e} seconds\n')

        # Print model summary
        self.print_model_summary(verbose)

    def _vector_fit(self, poles, omega, responses, weights, fit_constant, fit_proportional,
                    memory_saver, preserve_dc, max_iterations, stagnation_threshold, abstol):
        # This implements the core algorithm of vector fitting.
        # _vector_fit is called by vector_fit. For a description of the arguments see vector_fit.

        # Clear history. History variables are used to track convergence while fitting
        self._clear_history()

        # Initialize iteration counter, converged flag and max singular used in relocation loop
        iteration = 0

        # Pole relocation loop
        while True:
            logger.info(f'Iteration {iteration}')

            # Relocate poles
            poles, d_tilde = self._relocate_poles(
                poles, omega, responses, weights, fit_constant, fit_proportional, preserve_dc, memory_saver)

            # Check relative change of maximum singular value in A_dense stopping condition
            dRelMaxSv=self.delta_rel_max_singular_value_A_dense_history[-1]
            if dRelMaxSv < stagnation_threshold:
                logger.info(f'Stopping pole relocation because dRelMaxSv = {dRelMaxSv:.4e} < '
                            f'stagnation_threshold = {stagnation_threshold:.4e}')
                break

            # Check absolute error stopping condition only every 25 iterations
            if np.mod(iteration, 25) == 0:
                # Fit residues with the previously calculated poles
                residues, constant, proportional = self._fit_residues(
                    poles, omega, responses, weights, fit_constant, fit_proportional, preserve_dc)

                # Calculate error_max
                error_max = np.max(self._get_delta(poles, residues, constant, proportional, omega, responses, weights))

                logger.info(f'ErrorMax = {error_max:.4e}')

                # Check stopping condition
                if error_max < abstol:
                    logger.info(f'Stopping pole relocation because error_max = {error_max:.4e} < abstol = {abstol:.4e}')
                    break

            # Check maximum iterations stopping condition
            if iteration == max_iterations:
                logger.info(f'Stopping pole relocation because iteration = {iteration} '
                            f'== max_iterations = {max_iterations}')

                # Print convergence hint for cond(A_dense)
                max_cond = np.amax(self.history_cond_A_dense)
                if max_cond > 1e10:
                    warnings.warn('Hint: the linear system was ill-conditioned (max. condition number was '
                                    f'{max_cond:.4e}).')

                # Print convergence hint for rank(A_dense)
                max_deficiency = np.amax(self.history_rank_deficiency_A_dense)
                if max_deficiency < 0:
                   warnings.warn('Hint: the coefficient matrix was rank-deficient (max. rank deficiency was '
                                 f'{max_deficiency}).')

                break

            # Increment iteration counter
            iteration += 1

        # Fit residues with the previously calculated poles
        residues, constant, proportional = self._fit_residues(
            poles, omega, responses, weights, fit_constant, fit_proportional, preserve_dc)

        return poles, residues, constant, proportional

    def auto_fit(self,
                 # Initial poles
                 poles_init = None,
                 n_poles_init = None, poles_init_type = 'complex', poles_init_spacing = 'lin',

                 # Weighting
                 weights = None,
                 weighting: str = 'uniform',
                 weighting_accuracy_db: float = -60.0,

                 # Fit constant and/or proportional term
                 fit_constant: bool = True,
                 fit_proportional: bool = False,

                 # Share poles between responses
                 pole_sharing: str = 'MIMO',
                 pole_groups = None,

                 # Parameters for adding and skimming algorithm
                 n_poles_add_max: int = None, model_order_max: int = 1000,
                 # Note: n_iterations < 5 can lead to oscillation of the VF-AS algorithm with slow converge
                 n_iterations_pre: int = 5, n_iterations: int = 5, n_iterations_post: int = 5,
                 abstol: float = 1e-3, error_stagnation_threshold: float = 0.03, spurious_pole_threshold: float = 0.03,

                 # Memory saver
                 memory_saver: bool = False,

                 # Parameter type
                 parameter_type: str = 's',

                 # Verbose
                 verbose = False,

                 # Enforce dc data passivity
                 enforce_data_passivity_at_dc = True,

                 # Enable dc preserving fit
                 preserve_dc = True,
                 ) -> (np.ndarray, np.ndarray):
        """
        Automatic fitting routine implementing the "vector fitting with adding and skimming" algorithm as proposed in
        [#Grivet-Talocia]_. This algorithm is able to provide high quality macromodels with automatic model order
        optimization, while improving both the rate of convergence and the fit quality in case of noisy data.
        The resulting model parameters will be stored in the class variables :attr:`poles`, :attr:`residues`,
        :attr:`proportional` and :attr:`constant`.

        Parameters
        ----------
        poles_init: numpy array of initial poles, or list of numpy arrays of initial poles, optional.
            If specified, those poles will be used as initial poles.
            If specified as a list, the list elements correspond to the pole groups

        n_poles_init: int, or list of int, optional
            Number of poles in the initial model.
            If not specified, the number of initial poles will be estimated from the data
            If specified as a list, the list elements correspond to the pole groups

        poles_init_type: str, or list of str, optional
            Type of poles in the initial model. Can be 'complex' or 'real'. Only used if n_poles_init is specified.
            Otherwise the type of initial poles is estimated from the data.
            If specified as a list, the list elements correspond to the pole groups

        poles_init_spacing: str, or list of str, optional
            Spacing of the initial poles in the frequency range. Only used if n_poles_init is specified.
            Otherwise the poles will be estimated from the data.
            If specified as a list, the list elements correspond to the pole groups

        n_poles_add_max: int, optional
            Maximum number of new poles allowed to be added in each iteration. Controls how fast
            the model order is allowed to grow. If not specified, a reasonable value will be estimated from the data.

        model_order_max: int, optional
            Maximum model order to be used by the fit.

        n_iterations_pre: int, optional
            Number of initial iterations for pole relocation as in regular vector fitting.

        n_iterations: int, optional
            Number of intermediate iterations for pole relocation during each iteration of the adding and skimming loop.

        n_iterations_post: int, optional
            Number of final iterations for pole relocation after the adding and skimming loop terminated.

        weights: numpy ndarray of size n_responses, n_freqs, optional
            If weights are provided, these weights are used. The weights must be in the order
            The rows must be in this order: W11, W12, W13,...W21, W22,... where Wij is the weight
            vector used to calculate Sij * Wij.

            Alternatively to providing the weights yourself, you can set thei weighting parameter
            (and accompanying weighting_accuracy_db parameter) to have the weights created from the data.

        weighting: str, optional
            Weighting to be used for the frequency responses. The default is uniform weigthing.

            'uniform': Uniform weighting: Every frequency sample has the same weight. Favors absolute accuracy.
            Advantages: Ensures that no frequency range is prioritized over another.
            Disadvantages: May lead to poor accuracy in regions where the frequency response magnitude is much smaller.

            'inverse_magnitude': Inverse magnitude weighting: Weight is inversely proportional to the magnitude of the
            frequency response. Weight=1/abs(f(s)). Favors relative accuracy. Advantages: Improves the relative accuracy
            in low-magnitude regions, ensuring that small values in the response are not overshadowed by larger ones.
            Disadvantages: May overly emphasize small-magnitude regions, leading to less accuracy in high-magnitude
            regions or increased numerical sensitivity. Can amplify noise if the response magnitude is very small.

        weighting_accuracy_db: float, optional
            In inverse magnitude weighting, specifies a limit in dB (dB20), down to which the magnitudes are weighted.

            The possible range is -inf to 0 dB. If set to 0 dB, the minimum weight is 1, which is effectively the
            same as uniform weighting (all weights 1.0).

            Example: If you set it to -20 dB, all values are weighted according to their inverse magnitude 1/abs(value),
            but with a limit of 0.1: All values less than 0.1 are weighted with 1/0.1 but no less than that.

            In summary, with this parameter the accuracy tradeoff between large and small magnitudes can be adjusted.

            For example, if you want the same accuracy down to -80 dB as for larger values around 0 dB, you can set
            weighting_accuracy_db=-80. This will fit all values down to -80 dB with the same relative accuracy as those
            around 0 dB. Important note: It is inevitable that this will lead to a higher absolute RMS error of the
            entire fit. This is because the fit accuracy for larger values will now be traded off against the fit
            accuracy for small values.

            For this reason it is important not to set weighting_accuracy_db lower than you actually need, because
            otherwise you will sacrifice accuracy on the larger values for extra accuracy on very small values
            that you may not even be interested in. Note that setting it to, for example, -40 dB, also the -80 dB
            range values will benefit from more accuracy, but only to a certain extent limited by the -40 dB
            weighting_accuracy_db.

            The default was chosen to -60 dB because it may fit most practical applications, putting weight on values
            down to -60 dB but also not sacrificing too much accuracy in the 0 dB range.

        fit_constant: bool, optional
            Decide whether the constant term d is fitted or not.
            If preserve_dc is True, fit_constant is ignored and the DC point will always be set to the
            exact value of the data at DC.

        fit_proportional: bool, optional
            Decide whether the proportional term e is fitted or not.

        pole_sharing: str, optional
            Decide whether and in which way poles are shared between responses for the fit (pole groups)

            These options are available:
            'MIMO':
                All responses go into one shared pole group

            'Multi-SISO':
                Every response goes into a separate pole group

            'Multi-SIMO':
                Responses (1, 1) (2, 1) (3, 1) ... go into a pole group
                Responses (1, 2) (2, 2) (3, 2) ... go into a pole group
                ... and so on

            'Multi-MISO':
                Responses (1, 1) (1, 2) (1, 3) ... go into a pole group
                Responses (2, 1) (2, 2) (2, 3) ... go into a pole group
                ... and so on

            'Custom':
                You can create arbitrary custom pole groups by providing a matrix via the pole_groups argument.
                See description of pole_groups how it works

        pole_groups: numpy 2-d array of shape (n_ports, n_ports), optional
            Custom pole groups can be created by specifying pole_sharing='Custom' and providing the pole_groups
            matrix that contains integers.

            If all integers are distinct, every response will go into its own pole group.
            If some of the are equal, all of them will go into a common pole group.

            Pole groups will be ordered such that the smallest integer in the matrix will represent the
            first pole group and so on.

            Example 1: To achieve the same effect as in pole_sharing='Multi-SIMO' for a 3 x 3 network,
            you can provide the following pole_groups matrix:
            pole_groups=np.array(([0, 1, 2, 3], [0, 1, 2, 3], [0, 1, 2, 3]))

            Example 2: To put S11 and S13 into a separate pole group, and all other respones into
            another pole group, for a 3 x 3 network, you can provide the following pole_groups matrix:
            pole_groups=np.array(([0, 1, 0], [1, 1, 1], [1, 1, 1]))

        memory_saver: bool, optional
            Enables the memory saver. If enabled, the runtime might be longer but the memory usage is reduced.
            Use it for very large data sets if memory is the limiting factor.

        abstol: float, optional
            Absolute error threshold. The algorithm stops iterating once the absolute error of the fit is less than
            abstol.

        error_stagnation_threshold: float, optional
            Error stagnation treshold. The algorithm stops iterating if the decay of the error with respect to an
            average of the decay of the error over the last 3 iterations is less than
            error_stagnation_threshold * current error.

        spurious_pole_threshold: float, optional
            Threshold for the detection of spurious poles. A pole is skimmed if at least one of the integrals of the
            energy norm (l2 norm) of the corresponding pole-residue pairs contributes less than
            spurious_pole_threshold * mean_of_the_energy_norms_of_all_poles.

        parameter_type: str, optional
            Representation type of the frequency responses to be fitted. Either *scattering* (`'s'` or `'S'`),
            *impedance* (`'z'` or `'Z'`) or *admittance* (`'y'` or `'Y'`). It's recommended to perform the fit on the
            original S parameters. Otherwise, scikit-rf will convert the responses from S to Z or Y, which might work
            for the fit but can cause other issues.

        enforce_data_passivity_at_dc: bool, optional
            Enables the enforcement of the passivity of the DC point before fitting. The DC point cannot be modified
            by post-fit passivity enforcement because it needs to be exact. So the DC point of the fit will be just
            set to the DC point of the data. The model thus can only be passive if the DC point is passive as well.

            Circuit simulators numeric errors or measurments can lead to an (even sligthly) non-passive DC point.
            If you enable this setting, the DC point will be perturbed as slightly as possible (optimizer) until
            it is passive. This will lead to an error at DC but it is unavoidable to make the model passive at DC.

            A warning will be printed if the DC point is non-passive in any case, so you have the chance to provide
            better data at DC that is passive, avoiding subsequent errors due to the passivity enforcement.

        preserve_dc: bool, optional
            Enables a DC preserving with with modified rational basis functions. The DC point of the fit will be
            exactly the DC point of the data and the fit will not modify it.

            It is important to set enforce_data_passivity_at_dc to True because the passivity_enforce() algorithm
            will also not be able to modify the DC point if you fitted with preserve_dc = True. Thus, if the
            DC point in the data is not passive, it will be impossible to make the entire model passive.

        Returns
        -------
        None
            No return value.

        See Also
        --------
        vector_fit : Regular vector fitting routine.

        References
        ----------
        .. [#Grivet-Talocia] S. Grivet-Talocia and M. Bandinu, "Improving the convergence of vector fitting for
            equivalent circuit extraction from noisy frequency responses," in IEEE Transactions on Electromagnetic
            Compatibility, vol. 48, no. 1, pp. 104-120, Feb. 2006, DOI: https://doi.org/10.1109/TEMC.2006.870814
        """

        timer_start = timer()
        pole_sharing = pole_sharing.lower()

        # Get omega
        self.omega = self._get_omega_from_network()

        # Get responses
        self.responses = self._get_responses(parameter_type)
        n_responses = np.size(self.responses, axis=0)

        # Check and enforce passivity at DC
        self._check_and_enforce_data_passivity_at_dc(preserve_dc, enforce_data_passivity_at_dc)

        # Get weights
        if weights is None:
            self.weights=self._get_weights(weighting, weighting_accuracy_db)

        # Initialize pole sharing
        self._initialize_pole_sharing(pole_sharing, n_responses, pole_groups)
        n_pole_groups = len(self.poles)

        # Convert n_poles_init to list if it is provided as an int
        if not isinstance(n_poles_init, list):
            n_poles_init = [n_poles_init] * n_pole_groups

        # Convert poles_init_type to list if it is provided as a str
        if not isinstance(poles_init_type, list):
            poles_init_type = [poles_init_type] * n_pole_groups

        # Convert poles_init_spacing to list if it is provided as a str
        if not isinstance(poles_init_spacing, list):
            poles_init_spacing = [poles_init_spacing] * n_pole_groups

        # Convert poles_init to list if it is provided as a numpy array
        if poles_init is not None and not isinstance(poles_init, list):
            poles_init = [poles_init] * n_pole_groups

        # Print algorithm info message
        self._print_algorithm_info_messsage(preserve_dc, fit_constant, fit_proportional)

        # Save n_poles_add_max
        saved_n_poles_add_max = n_poles_add_max

        # Fit each pole group
        for idx_pole_group in range(n_pole_groups):
            # Get the indices of all responses that are part of the pole group
            indices_responses=np.nonzero(self.map_idx_response_to_idx_pole_group == idx_pole_group)

            logger.info(f'Starting auto_fit for pole group {idx_pole_group+1} of {n_pole_groups}')

            # Initialize poles
            if poles_init is None:
                # Get initial poles
                poles = self._get_initial_poles(
                    self.omega, n_poles_init[idx_pole_group], poles_init_type[idx_pole_group],
                    poles_init_spacing[idx_pole_group], self.responses[indices_responses])
            else:
                # Set initial poles to user-provided poles
                poles = poles_init[idx_pole_group]

            # Initialize n_poles_add_max. We have to do this for every pole group because
            # it is set proportional to the number of poles if no user value is provided
            # number of poles w
            if saved_n_poles_add_max is None:
                n_poles_add_max=max(2, int(len(poles)/2))
            else:
                # Set n_poles_add_max to user-provided value
                n_poles_add_max = saved_n_poles_add_max

            # Call _auto_fit
            poles, residues, constant, proportional = self._auto_fit(
                poles, self.omega, self.responses[indices_responses], self.weights[indices_responses],
                fit_constant, fit_proportional, preserve_dc, memory_saver,
                n_iterations_pre, n_iterations, n_iterations_post,
                error_stagnation_threshold, spurious_pole_threshold,
                abstol, model_order_max, n_poles_add_max)

            # Save results
            self._save_results(
                poles, residues, constant, proportional, idx_pole_group)

            logger.info(f'Finished auto_fit for pole group {idx_pole_group+1} of {n_pole_groups}')

        wall_clock_time = timer() - timer_start
        logger.info(f'Finished auto_fit. Time elapsed = {wall_clock_time:.4e} seconds\n')

        # Print model summary
        self.print_model_summary(verbose)

    def _auto_fit(self,
        poles, omega, responses, weights,
        fit_constant, fit_proportional, preserve_dc, memory_saver,
        n_iterations_pre, n_iterations, n_iterations_post,
        error_stagnation_threshold, spurious_pole_threshold, abstol, model_order_max, n_poles_add_max):

        # This implements the core algorithm of vector fitting with adding and skimming.
        # _auto_fit is called by auto_fit. For a description of the arguments see auto_fit.

        # Clear history. History variables are used to track convergence while fitting
        self._clear_history()

        # Initial pole relocation
        logger.info('Initial pole relocation')
        for _ in range(n_iterations_pre):
            poles, d_tilde = self._relocate_poles(
                poles, omega, responses, weights,
                fit_constant, fit_proportional, preserve_dc, memory_saver)

        # Fit residues
        residues, constant, proportional = self._fit_residues(
            poles, omega, responses, weights, fit_constant, fit_proportional, preserve_dc)

        # Calculate delta
        delta = self._get_delta(poles, residues, constant, proportional, omega, responses, weights)

        # Initialize stopping condition variables of skim-and-add loop
        error_max = np.max(delta)
        model_order = np.sum((poles.imag != 0) + 1)
        delta_eps_avg_m = 3 # Average the last m iterations of delta eps
        error_max_history = []
        iteration=0

        # Minimum spacing of added poles to existing poles
        delta_omega_min = (omega[1] - omega[0]) * 1.0

        # Pole skimming and adding loop
        while True:
            logger.info(f'AS-Loop Iteration = {iteration} AbsError = {error_max:.4e}, ModelOrder = {model_order}')

            if error_max <= abstol:
                logger.info(f'Stopping AS-Loop because AbsError = {error_max:.4e} < AbsTol = {abstol:.4e}')
                break

            # Get spurious poles and skim
            spurious = self.get_spurious(poles, residues, spurious_pole_threshold)
            poles = poles[~spurious]

            # Get pole candidates
            pole_candidates = self._get_pole_candidates(delta, omega)

            # Calculate how many of the pole candidates we will add
            n_pole_candidates = len(pole_candidates)
            n_poles_skimmed = np.sum(spurious)
            n_poles_add=min(n_pole_candidates, n_poles_skimmed+n_poles_add_max)

            logger.info(f'n_poles_skimmed = {n_poles_skimmed} '
                        f'n_pole_candidates = {n_pole_candidates} n_poles_add = {n_poles_add}')

            # Merge pole_candidates into poles keeping a minimum distance delta_omega_min to existing poles.
            # If they collide, the candidates will be moved to the best possible available spot.
            poles = self._add_poles(poles, pole_candidates[:n_poles_add], delta_omega_min)

            # Intermediate pole relocation
            logger.info('Intermediate pole relocation')
            for _ in range(n_iterations):
                poles, d_tilde, = self._relocate_poles(
                    poles, omega, responses, weights,
                    fit_constant, fit_proportional, preserve_dc, memory_saver)

            # Fit residues
            residues, constant, proportional = self._fit_residues(
                poles, omega, responses, weights, fit_constant, fit_proportional, preserve_dc)

            # Calculate delta
            delta = self._get_delta(poles, residues, constant, proportional, omega, responses, weights)

            # Update stopping condition variables
            error_max=np.max(delta)
            error_max_history.append(error_max)
            if len(error_max_history) >= delta_eps_avg_m:
                # Calculate delta_error using the last delta_eps_avg_m values
                delta_error=np.diff(error_max_history[-delta_eps_avg_m:])
                # Set all delta error that are positive to 0. This means that
                # if the error gets larger, we have 0 improvement.
                delta_error=np.clip(delta_error, -np.inf, 0)
                # It can happen that all elements have been clipped, put at least one element in
                if len(delta_error) == 0:
                    delta_error=np.array([0])
                # Calculate the mean of the absolute values
                delta_eps = np.mean(np.abs(delta_error))
                delta_eps_min = error_stagnation_threshold * error_max
                # Check delta_eps stopping condition
                if delta_eps < delta_eps_min:
                    logger.info(f'Stopping AS-Loop because DeltaEps = {delta_eps:.4e} < '
                                f'DeltaEpsMin = {delta_eps_min:.4e}')
                    break
            model_order = np.sum((poles.imag != 0) + 1)

            # Check stopping condition of model order
            if model_order >= model_order_max:
                logger.info(f'Stopping AS-Loop because ModelOrder = {model_order} >= '
                            f'ModelOrderMax = {model_order_max}')
                break

            iteration += 1

        # Final skimming of spurious poles
        logger.info('Final pole relocation')
        spurious = self.get_spurious(poles, residues, spurious_pole_threshold)
        poles = poles[~spurious]
        n_poles_skimmed = np.sum(spurious)
        logger.info(f'n_poles_skimmed = {n_poles_skimmed}')

        # Final pole relocation
        for _ in range(n_iterations_post):
            poles, d_tilde = self._relocate_poles(
                poles, omega, responses, weights,
                fit_constant, fit_proportional, preserve_dc, memory_saver)

        # Final residue fitting
        residues, constant, proportional = self._fit_residues(
            poles, omega, responses, weights,
            fit_constant, fit_proportional, preserve_dc)

        return poles, residues, constant, proportional

    def _initialize_pole_sharing(self, pole_sharing, n_responses, pole_groups):
        # Initializes all data structures needed for pole sharing

        # Create pole group indices and pole group member indices. Both have n_responses elements
        # and map idx_response to idx_pole_group and idx_pole_group_member.
        #
        # idx_pole_group is the index of the pole group of the response. It is used to index self.poles,
        # self.residues, self.constant and self.proportional. Each of them contains a numpy array.
        #
        # idx_pole_group_member is used as an index on the numpy array in self.residues[idx_pole_group]
        # to get the residues of a response.
        #
        # Note: The order of n_responses is S11, S12, ..., S21, S22, ...
        if pole_sharing == 'mimo':
            n_pole_groups = 1
            # Every response goes in the same pole group 0
            self.map_idx_response_to_idx_pole_group = np.zeros(n_responses, dtype=int)
            # The order of the responses inside of pole group 0 is S11, S12, ..., S21, S22, ...
            self.map_idx_response_to_idx_pole_group_member = np.arange(n_responses, dtype=int)

        elif pole_sharing == 'multi-siso':
            n_pole_groups = n_responses
            # Every response goes into its own pole group
            self.map_idx_response_to_idx_pole_group = np.arange(n_responses, dtype=int)
            # Every pole group contains only one response
            self.map_idx_response_to_idx_pole_group_member = np.zeros(n_responses, dtype=int)

        elif pole_sharing == 'multi-simo':
            n_ports = int(np.sqrt(n_responses))
            n_pole_groups = n_ports
            # We have n_ports pole groups
            # Responses S11, S21, S31, ... go into pole group 0
            # Responses S12, S22, S32, ... go into pole group 1 and so on
            self.map_idx_response_to_idx_pole_group = np.tile(np.arange(n_ports, dtype=int), n_ports)
            # Every pole group contains n_ports responses.
            self.map_idx_response_to_idx_pole_group_member = np.repeat(np.arange(0, n_ports, dtype=int), n_ports)

        elif pole_sharing == 'multi-miso':
            n_ports = int(np.sqrt(n_responses))
            n_pole_groups = n_ports
            # We have n_ports pole groups
            # Responses S11, S12, S13, ... go into pole group 0
            # Responses S21, S22, S23, ... go into pole group 1 and so on
            self.map_idx_response_to_idx_pole_group = np.repeat(np.arange(0, n_ports, dtype=int), n_ports)
            # Every pole group contains n_ports responses.
            self.map_idx_response_to_idx_pole_group_member = np.tile(np.arange(n_ports, dtype=int), n_ports)

        elif pole_sharing == 'custom':
            n_ports = int(np.sqrt(n_responses))
            # Check correct shape of pole_groups
            if not np.shape(pole_groups) == (n_ports, n_ports):
                raise RuntimeError('Custom port groups matrix needs to be of shape (n_ports, n_ports)')
            # Get number of unique values in pole_groups, which is n_pole_groups
            sorted_unique_values = np.unique(pole_groups)
            # Create dict mapping each unique value to idx_pole_group
            map_value_to_idx_pole_group = {value: idx for idx, value in enumerate(sorted_unique_values)}
            # Get number of pole groups
            n_pole_groups = len(sorted_unique_values)
            # Initialize maps
            self.map_idx_response_to_idx_pole_group = np.empty((n_responses), dtype=int)
            self.map_idx_response_to_idx_pole_group_member = np.empty((n_responses), dtype=int)
            # Initialize member counters to zero. For each group we have a counter than will be incremented
            # while scanning through pole_groups if we find a response that is part of this group
            member_counters=np.zeros(n_pole_groups)
            # Scan pole_groups
            for i in range(n_ports):
                for j in range(n_ports):
                    idx_response = i * n_ports + j
                    value=pole_groups[i, j]
                    idx_pole_group=map_value_to_idx_pole_group[value]
                    self.map_idx_response_to_idx_pole_group[idx_response]=idx_pole_group
                    self.map_idx_response_to_idx_pole_group_member[idx_response]=member_counters[idx_pole_group]
                    # Increment member counter
                    member_counters[idx_pole_group] += 1

        else:
            warnings.warn('Invalid choice of pole_sharing. Proceeding with pole_sharing=\'MIMO\'',
                          UserWarning, stacklevel=2)
            n_pole_groups = 1
            # Every response goes in the same pole group 0
            self.map_idx_response_to_idx_pole_group = np.zeros(n_responses, dtype=int)
            # The order of the responses inside of pole group 0 is S11, S12, ..., S21, S22, ...
            self.map_idx_response_to_idx_pole_group_member = np.arange(n_responses, dtype=int)

        # Initialize data structures for results
        self.poles = [None] * n_pole_groups
        self.residues = [None] * n_pole_groups
        self.residues_modified = [None] * n_pole_groups
        self.constant = [None] * n_pole_groups
        self.constant_modified = [None] * n_pole_groups
        self.proportional = [None] * n_pole_groups

    def _get_initial_poles(self, omega: list, n_poles: int, pole_type: str, pole_spacing: str, responses):
        # Create initial poles and space them across the frequencies
        #
        # According to Gustavsen they thould generally
        # be complex conjugate pole pairs with linear spacing. Real poles
        # only work for very smooth responses.
        #
        # Note: According to the VF-AS paper, placing multiple poles at the same
        # frequency will lead to a seriously ill conditioned least squares system.

        poles=[]

        if n_poles is None:
            # Estimate the initial poles from the responses

            # Calculate absolute value of responses
            responses_abs=np.abs(responses)

            # Mean over all responses
            responses_abs=np.mean(responses_abs, axis=0)

            # Subtract the mean response. This is not important to find the peaks but it increases
            # numerical accuracy.
            responses_abs=responses_abs-np.mean(responses_abs)

            # Find peaks. The prominence is adjusted according to the selected abstol to avoid
            # placing way too many poles for deviations in the responses that are less than abstol.
            idx_peaks, _ = find_peaks(responses_abs,
                                      prominence=0.05*(np.max(responses_abs)-np.min(responses_abs)))

            # Plot for debug
            # import matplotlib.pyplot as plt
            # plt.plot(responses_abs)
            # plt.plot(idx_peaks, responses_abs[idx_peaks], "x")
            # plt.plot(np.zeros_like(responses_abs), "--", color="gray")
            # plt.show()

            poles = omega[idx_peaks]

            # Check if the peak finder failed. This can happen if the response is completely
            # smooth and no maximum exists.
            if len(poles) == 0:
                # In the case of smooth responses, according to Gustavsen, it is better to
                # use real poles instead. The real poles will be created below.
                pole_type='real'

                # Create two real poles
                n_poles = 2
                logger.info(f'Automatic initial pole estimation created {n_poles} {pole_type} poles')
            else:
                logger.info(f'Automatic initial pole estimation created {len(poles)} {pole_type} poles')

        if len(poles) == 0:
            # Either n_poles is set or the automatic initial pole estimation failed.

            # Space out the poles linearly or logarithmically over the frequency range
            omega_min = np.amin(omega)
            omega_max = np.amax(omega)

            # Poles cannot be at f=0; hence, f_min for starting pole must be greater than 0
            if omega_min == 0.0:
                omega_min = omega[1]

            pole_spacing = pole_spacing.lower()
            if pole_spacing == 'log':
                poles = np.geomspace(omega_min, omega_max, n_poles)

            elif pole_spacing == 'lin':
                poles = np.linspace(omega_min, omega_max, n_poles)

            else:
                warnings.warn('Invalid choice of initial pole spacing; proceeding with linear spacing.',
                              UserWarning, stacklevel=2)
                poles = np.linspace(omega_min, omega_max, n_poles)

        # Multiply by -1 for real poles or by (-0.01+1j) for complex poles
        pole_type = pole_type.lower()
        if pole_type == 'real':
            poles = -1.0 * poles

        elif pole_type == 'complex':
            poles = (-0.01 + 1j) * poles

        else:
            warnings.warn('Invalid choice of initial pole type; proceeding with complex poles.',
                          UserWarning, stacklevel=2)
            poles=(-0.01 + 1j) * poles

        return poles

    def _clear_history(self):
        # Clears global history variables
        self.d_tilde_history = []
        self.delta_rel_max_singular_value_A_dense_history = []
        self.history_cond_A_dense = []
        self.history_rank_deficiency_A_dense = []
        self.max_singular_value_A_dense = 1

    def _get_omega_from_network(self):
        # Calculates omega
        omega = 2.0 * np.pi * np.array(self.network.f)
        return omega

    def _get_netlist_header(self, simulator: str = 'Xyce',
                            create_reference_pins: bool = False,
                            fitted_model_name: str = 's_equivalent'):
        # Returns a netlist header

        # Get frequency spacing
        contains_dc, f_min, f_max, sweep_type, n_points = self._get_frequency_spacing()

        simulator = simulator.lower()
        header = ''
        name = self.network.name
        n_ports = self.network.nports
        if simulator == 'xyce':
            header += '* Example how to use this model in a simulation in Xyce:\n'

            if sweep_type == 'lin':
                header += f'.AC LIN {int(n_points)} {f_min:.0f} {f_max:.0f}\n'
            else:
                header += f'.AC DEC {int(n_points)} {f_min:.0f} {f_max:.0f}\n'

            header += f'.LIN FORMAT=TOUCHSTONE2 LINTYPE=S DATAFORMAT=MA FILE={name}-xyce.s4p '
            header += 'WIDTH=15 PRECISION=12\n'

            header += '*.TRAN 1ps 1000ns\n'

            # Create subcircuit pins
            if create_reference_pins:
                str_input_nodes = " ".join(map(lambda x: f'nt_p{x + 1} 0', range(n_ports)))
            else:
                str_input_nodes = " ".join(map(lambda x: f'nt_p{x + 1}', range(n_ports)))

            header += f'Xdut {str_input_nodes} {fitted_model_name}\n'
            str_port_instances = "\n".join(map(lambda x:
                f'P{x + 1} nt_p{x + 1} 0 dc 0 port={x + 1} Z0={np.real(self.network.z0[0, x])} ac 1 SIN(0 1 1e9) ', range(n_ports)))
            header += str_port_instances
            header += '\n\n'

        return header

    def _get_frequency_spacing(self):
        # Returns the type of frequency spacing that is used.

        # Get frequencies
        f=np.array(self.network.f)

        if f[0] == 0:
            contains_dc = True
            idx_first = 1
        else:
            contains_dc = False
            idx_first = 0

        # Get deltas
        delta1 = f[idx_first + 1] - f[idx_first]
        delta2 = f[idx_first + 3] - f[idx_first + 2]

        # Get span
        f_min = f[idx_first]
        f_max = f[-1]

        if np.abs(delta1-delta2) < 1e-12:
            sweep_type = 'lin'
            # Number of points
            n_points = len(f) - idx_first
        else:
            sweep_type = 'log'
            # Number of points per decade
            n_points = 1 / np.log10(f[idx_first+1]/f[idx_first])

        return contains_dc, f_min, f_max, sweep_type, n_points

    def _all_proportional_are_zero(self):
        # Checks if all proportional terms are zero
        all_proportional_are_zero=True
        for proportional in self.proportional:
            if not len(np.flatnonzero(proportional)) == 0:
                all_proportional_are_zero=False
                break
        return all_proportional_are_zero

    def _get_n_responses(self, idx_pole_group = None) -> int:
        # Returns the number of responses

        if idx_pole_group is None:
            return len(self.map_idx_response_to_idx_pole_group)
        else:
            return np.size(self.residues[idx_pole_group], axis = 0)

    def get_n_poles_complex(self, idx_pole_group = None) -> int:
        # Returns the number of complex poles

        if idx_pole_group is None:
            pole_group_indices = np.arange(len(self.poles))
        else:
            pole_group_indices = np.array([idx_pole_group])

        n_poles_complex = 0
        for idx_pole_group in pole_group_indices:
            n_poles_complex += np.sum(self.poles[idx_pole_group].imag > 0)

        return n_poles_complex

    def _save_results(self,
        poles, residues, constant, proportional, idx_pole_group):
        # Saves the results

        self.poles[idx_pole_group] = poles
        self.residues[idx_pole_group] = residues
        self.constant[idx_pole_group] = constant
        self.proportional[idx_pole_group] = proportional

    def get_n_poles_real(self, idx_pole_group = None) -> int:
        # Returns the number of real poles

        if idx_pole_group is None:
            pole_group_indices = np.arange(len(self.poles))
        else:
            pole_group_indices = np.array([idx_pole_group])

        n_poles_real = 0
        for idx_pole_group in pole_group_indices:
            n_poles_real += np.sum(self.poles[idx_pole_group].imag == 0)

        return n_poles_real

    def print_model_summary(self, verbose = False):
        # Prints a model summary

        n_ports=self._get_n_ports()
        print('Model summary:')

        n_pole_groups=len(self.poles)

        if not verbose:
            print('Run print_model_summary(verbose=True) for an extended summary')
        else:
            for idx_pole_group in range(n_pole_groups):
                indices_responses=np.nonzero(self.map_idx_response_to_idx_pole_group == idx_pole_group)[0]
                i = np.floor(indices_responses / n_ports).astype(int)
                j = np.mod(indices_responses, n_ports)
                member_responses = ' '.join(map(lambda x: f'({i[x] + 1}, {j[x] + 1})', range(len(i))))
                print(f'Pole group {idx_pole_group+1}:')
                print(f'\tModel order = {self.get_model_order(idx_pole_group)}')
                print(f'\tNumber of real poles = {self.get_n_poles_real(idx_pole_group)}')
                print(f'\tNumber of complex conjugate pole pairs = {self.get_n_poles_complex(idx_pole_group)}')
                print(f'\tMember responses = {member_responses}')

            if self.network:
                abs_err=self.get_abs_error_vs_responses()
                rel_err=self.get_rel_error_vs_responses()
                for i in range(n_ports):
                    for j in range(n_ports):
                        print(f'Response ({i+1}, {j+1}): AbsErr={abs_err[i, j]:.4e} RelErr={rel_err[i, j]:.4e}')

        print(f'Number of ports = {n_ports}')
        print(f'Number of responses = {n_ports**2}')
        print(f'Model order = {self.get_model_order()}')
        print(f'Number of real poles = {self.get_n_poles_real()}')
        print(f'Number of complex conjugate pole pairs = {self.get_n_poles_complex()}')
        print(f'Number of pole groups = {n_pole_groups}')

        if self.network:
            print(f'Total absolute error (RMS) = {self.get_total_abs_error():.4e}')
            print(f'Total relative error (RMS) = {self.get_total_rel_error():.4e}')
            if verbose and n_ports <= 4:
                self.plot_model_vs_data()

        if self.is_symmetric():
            print('Model is symmetric = True')
        else:
            print('Model is symmetric = False')

        if self._all_proportional_are_zero():
            print(f'Model is passive = {self.is_passive()}')
        else:
            print('Model is passive = unknown (Passivity test works only for models without proportional terms)')

    def _get_n_ports(self, idx_pole_group = None):
        # Returns the number of ports derived from self.map_idx_response_to_idx_pole_group
        if idx_pole_group is None:
            n_ports = int(np.sqrt(len(self.map_idx_response_to_idx_pole_group)))
        else:
            n_ports=int(np.sqrt(self._get_n_responses(idx_pole_group)))

        return n_ports

    def _get_weights(self, weighting, weighting_accuracy_db):
        # Calculates the weights w(s)

        if weighting.lower() == 'uniform':
            weights=np.ones(np.shape(self.responses))

        elif weighting.lower() == 'inverse_magnitude':
            weights = 1/np.clip(np.abs(self.responses), np.pow(10, weighting_accuracy_db/20), np.inf)

        else:
            warnings.warn('Invalid choice of weighting. Proceeding with uniform weighting'
                           ,UserWarning, stacklevel=2)
            weights=np.ones(np.shape(self.responses))

        return weights

    def _get_responses(self, parameter_type: str = 's'):
        # Get responses in vector fitting format

        # Get network responses
        nw_responses=self._get_responses_from_network(parameter_type)

        # Stack frequency responses as a single vector
        # stacking order (row-major):
        # s11, s12, s13, ..., s21, s22, s23, ...
        responses = []
        for i in range(self.network.nports):
            for j in range(self.network.nports):
                responses.append(nw_responses[:, i, j])
        responses = np.array(responses)

        return responses

    def _get_responses_from_network(self, parameter_type: str = 's'):
        # Get network responses

        # Select network representation type
        parameter_type=parameter_type.lower()
        if parameter_type == 's':
            nw_responses = self.network.s
        elif parameter_type == 'z':
            nw_responses = self.network.z
        elif parameter_type == 'y':
            nw_responses = self.network.y
        else:
            warnings.warn('Invalid choice of matrix parameter type (S, Z, or Y); proceeding with scattering '
                          'representation.', UserWarning, stacklevel=2)
            nw_responses = self.network.s

        return nw_responses

    @staticmethod
    def _add_poles(poles, poles_add, delta_omega_min):
        # Adds poles_add into poles in order of poles_add, keeping a minimum
        # delta_omega_min between each add-pole and all other poles.
        # If an add-pole falls within the zone of an already existing pole,
        # it is moved and inserted either to the right or left of the existing
        # pole's zone, depending on which is closer add-pole.

        # Step 1: Sort original poles according to their norm. This step is equivalent
        # to rotating the poles onto the imaginary axis, as described in the VF-AS paper.
        i_sort = np.argsort(np.abs(poles))
        poles=poles[i_sort]

        # Step 2: Create zones for each pole, defined by the beginning (b)
        # and end (e) for each zone
        zones_begin = np.abs(poles) - delta_omega_min
        zones_end = np.abs(poles) + delta_omega_min

        # Convert to list so we can append
        poles=poles.tolist()
        zones_begin=zones_begin.tolist()
        zones_end=zones_end.tolist()

        # Merge overlapping zones if we have more than 1 zone
        if len(zones_begin) > 1:
            # Merged zones will be collected
            zones_merged_begin = []
            zones_merged_end = []
            # Initialize zone candidate for merging
            zone_candidate_begin=zones_begin[0]
            zone_candidate_end=zones_end[0]
            # Iterate over all zones and possibly merge multiple consecutive zones
            for i in np.arange(start=1, stop=len(zones_begin)):
                # Check if candidate overlaps into next zone
                if zone_candidate_end > zones_begin[i]:
                    # Merge the candidate and the next zone into new candidate
                    zone_candidate_end = zones_end[i]
                else:
                    # No overlap. Save the candidate zone
                    zones_merged_begin.append(zone_candidate_begin)
                    zones_merged_end.append(zone_candidate_end)
                    # Set new candidate to next zone
                    zone_candidate_begin=zones_begin[i]
                    zone_candidate_end=zones_end[i]
            # Overwrite original zones with merged zones
            zones_begin=zones_merged_begin
            zones_end=zones_merged_end

        # Step 3: For each add-pole p
        if len(zones_begin) > 0:
            for p in poles_add:
                # Rotate p onto imaginary axis. p_abs is used for all comparisons
                # with the zones, but the complex valued p is actually appended
                # to poles
                p_abs=np.abs(p)

                # Make sure that p is not too close to the origin
                if p_abs < delta_omega_min:
                    p=(-0.01 + 1j) * delta_omega_min
                    p_abs=np.abs(p)

                # Directly prepend the pole if it is below the first zone
                if p_abs < zones_begin[0]:
                    # Merge new zone with first zone if it overlaps
                    if p_abs+delta_omega_min > zones_begin[0]:
                        zones_begin[0]=p_abs-delta_omega_min
                    else:
                        # Create new zone on index 0
                        zones_begin.insert(0, p_abs-delta_omega_min)
                        zones_end.insert(0, p_abs+delta_omega_min)
                    # Append pole and process next pole
                    poles.append(p)
                    continue

                # Directly append the pole if it is above the last zone
                if p_abs > zones_end[-1]:
                    # Merge new zone with last zone if it overlaps
                    if p_abs-delta_omega_min < zones_end[-1]:
                        zones_end[-1]=p_abs+delta_omega_min
                    else:
                        # Append new zone
                        zones_begin.append(p_abs-delta_omega_min)
                        zones_end.append(p_abs+delta_omega_min)
                    # Append pole and process next pole
                    poles.append(p)
                    continue

                # Step 3a: Find the first index idx in zones_begin, for which p < b
                # Note: If p < zones_begin is not true for any b, idx=0 will be returned.
                # In this case, because we already have handled the case p < zones_begin[0]
                # above, we know that p > zones_begin[-1]
                idx=np.argmax(p_abs < zones_begin)

                # Handle special case where idx=0
                if idx == 0:
                    # Check if p > zone_e[-1]
                    if p_abs > zones_end[-1]:
                        # Merge or insert
                        if p_abs-delta_omega_min < zones_end[-1]:
                            # Merge new zone with left zone if it overlaps
                            zones_end[-1]=p_abs+delta_omega_min
                        else:
                            # Append new zone
                            zones_begin.append(p_abs-delta_omega_min)
                            zones_end.append(p_abs+delta_omega_min)
                    # Otherwise p is inside the last zone
                    else:
                        # Check distance from p to b and e of last zone
                        if zones_end[-1] - p_abs >= p_abs - zones_begin[-1]:
                            # Abut on the left of zone -1
                            p=(-0.01 + 1j) * zones_begin[-1]
                            p_abs=np.abs(p)

                            # The new zone overlaps into the last zone, so we merge them
                            zones_begin[-1]=p_abs-delta_omega_min
                            # Check if the merged zone overlaps on the left
                            if len(zones_begin) > 1:
                                if zones_begin[-1] < zones_end[-2]:
                                    zones_end[-2]=zones_end[-1]
                                    del zones_end[-1]
                                    del zones_begin[-1]
                        else:
                            # Abut on the right of zone -1
                            p=(-0.01 + 1j) * zones_end[-1]
                            p_abs=np.abs(p)
                            # The new zone overlaps into the last zone, so we merge them
                            zones_end[-1]=p_abs+delta_omega_min
                    # Append pole and process next pole
                    poles.append(p)
                    continue

                # Step 3b: We know now the index at which we would insert the new zone
                # for p but we only know that p is between zone_b[idx-1] and
                # zone_b[idx], so we have to distinguish two cases:

                # Check if p > zone_e[idx-1] (p is not inside of left zone)
                if p_abs > zones_end[idx-1]:
                    # Merge or insert
                    if p_abs-delta_omega_min < zones_end[idx-1]:
                        # Merge new zone with left zone
                        zones_end[idx-1]=p_abs+delta_omega_min
                    else:
                        # Insert new zone before index idx
                        zones_begin.insert(idx, p_abs-delta_omega_min)
                        zones_end.insert(idx, p_abs+delta_omega_min)

                    # The new zone or the merged zone is now at idx-1
                    # Check if the new or merged zone overlaps on the right
                    if idx <= len(zones_begin)-1:
                        if zones_end[idx-1] > zones_begin[idx]:
                            zones_end[idx-1]=zones_end[idx]
                            del zones_end[idx]
                            del zones_begin[idx]

                    # Append pole and process next pole
                    poles.append(p)
                    continue

                # Otherwise p is inside the left zone
                else:
                    # Check distance from p to b and e of left zone
                    if zones_end[idx-1] - p_abs >= p_abs - zones_begin[idx-1]:
                        # Abut on the left of zone idx-1
                        p=(-0.01 + 1j) * zones_begin[idx-1]
                        p_abs=np.abs(p)
                        # The new zone overlaps into the idx-1 zone, so we merge them
                        zones_begin[idx-1]=p_abs-delta_omega_min
                        # Check if the merged zone overlaps on the left
                        if idx-2 >= 0:
                            if zones_begin[idx-1] < zones_end[idx-2]:
                                zones_end[idx-2]=zones_end[idx-1]
                                del zones_end[idx-1]
                                del zones_begin[idx-1]
                    else:
                        # Abut on the right of zone idx-1
                        p=(-0.01 + 1j) * zones_end[idx-1]
                        p_abs=np.abs(p)
                        # The new zone overlaps into the idx-1 zone, so we merge them
                        zones_end[idx-1]=p_abs+delta_omega_min
                        # Check if the merged zone overlaps on the right
                        if idx <= len(zones_begin)-1:
                            if zones_end[idx-1] > zones_begin[idx]:
                                zones_end[idx-1]=zones_end[idx]
                                del zones_end[idx]
                                del zones_begin[idx]
                    # Append pole and process next pole
                    poles.append(p)
                    continue

                # If we reached the end of this loop, there is a bug in the code
                raise RuntimeError('Error in _add_poles')

        # Convert back to np array and return
        return np.array(poles)

    def _get_rational_basis_functions(self, s, poles, modified = False):
        # Returns the rational basis functions and indices
        # If modified is True: Returns the basis functions for modified vector fitting (s*r/(s-p))
        # If modified is False: Returns the basis functions for original vector fitting (r/(s-p))

        # Get indices of poles
        idx_poles_real, idx_poles_complex = self._get_indices_poles(poles)

        # Create rbf indices
        n_poles_real = len(idx_poles_real)
        n_poles_complex = len(idx_poles_complex)
        idx_rbf_re = np.arange(n_poles_real)
        idx_rbf_complex_re = n_poles_real + 2 * np.arange(n_poles_complex)
        idx_rbf_complex_im = idx_rbf_complex_re + 1

        if modified:
            # Build components of rational basis functions (RBF)
            rbf_real = s[:, None] / (s[:, None] - poles[None, idx_poles_real])

            rbf_complex_re = (s[:, None] / (s[:, None] - poles[None, idx_poles_complex]) +
                                s[:, None] / (s[:, None] - np.conj(poles[None, idx_poles_complex])))
            rbf_complex_im = ((1j * s[:, None]) / (s[:, None] - poles[None, idx_poles_complex]) -
                                (1j * s[:, None]) / (s[:, None] - np.conj(poles[None, idx_poles_complex])))
        else:
            # Build components of rational basis functions (RBF)
            rbf_real = 1 / (s[:, None] - poles[None, idx_poles_real])

            rbf_complex_re = (1 / (s[:, None] - poles[None, idx_poles_complex]) +
                                1 / (s[:, None] - np.conj(poles[None, idx_poles_complex])))
            rbf_complex_im = (1j / (s[:, None] - poles[None, idx_poles_complex]) -
                                1j / (s[:, None] - np.conj(poles[None, idx_poles_complex])))


        return rbf_real, rbf_complex_re, rbf_complex_im, idx_rbf_re, idx_rbf_complex_re, idx_rbf_complex_im

    def _get_R22_equation_system(self,
        responses, weights, poles, omega,
        fit_constant, fit_proportional,
        preserve_dc, memory_saver):

        s = 1j * omega

        n_responses, n_freqs = np.shape(responses)
        n_samples = n_responses * n_freqs

        # Get total number of poles, counting complex conjugate pairs as 2 poles
        n_poles = np.sum((poles.imag != 0) + 1)

        # Initialize number of elements in C
        n_C = n_poles

        # Get index of constant term if we have it
        if not preserve_dc and fit_constant:
            idx_const = [n_C]
            n_C += 1

        # Get index of proportional term if we have it
        if fit_proportional:
            idx_prop = [n_C]
            n_C += 1

        # Number of elements in C~ = C_tilde
        if preserve_dc:
            n_C_tilde = n_poles
        else:
            # Need + 1 for d~ = d_tilde
            n_C_tilde = n_poles + 1

        # Calculate n_rows of R22
        K = min(n_freqs * 2, n_C + n_C_tilde)
        n_rows_R22 = K - n_C

        # Initialize R22
        R22 = np.empty((n_responses, n_rows_R22, n_C_tilde))

        # Initialize RHS. We only need it for preserve_dc. Otherwise RHS = 0. In this case
        # b_dense will be created directly using np.zeros later instead of reshaping RHS.
        if preserve_dc:
            # RHS = Q^T (H-d)
            RHS = np.empty((n_responses, n_rows_R22))

        # Get rational basis functions (RBF)
        rbf_real, rbf_complex_re, rbf_complex_im, idx_rbf_re, idx_rbf_complex_re, idx_rbf_complex_im = \
            self._get_rational_basis_functions(s, poles, preserve_dc)

        if not memory_saver:
            # We build all rows of A at once and run the QR factorization using
            # numpy. I guess that numpy will use multiple threads to parallelize.
            #
            # Matrix A can be pretty big because it is of size:
            # n_responses*n_freqs*(n_C + n_C_tilde)
            #
            # If A is too big, we can also compute the QR factorization for
            # each row of A serially and save only the resulting R22.
            # This is done if memory_saver == True

            A = np.empty((n_responses, n_freqs, n_C + n_C_tilde), dtype=complex)

            # Components W X
            A[:, :, idx_rbf_re] = weights[:, :, None] * rbf_real[None, :, :]
            A[:, :, idx_rbf_complex_re] = weights[:, :, None] * rbf_complex_re[None, :, :]
            A[:, :, idx_rbf_complex_im] = weights[:, :, None] * rbf_complex_im[None, :, :]
            if not preserve_dc and fit_constant:
                A[:, :, idx_const] = 1 * weights[:, :, None]
            if fit_proportional:
                A[:, :, idx_prop] = weights[:, :, None] * s[None, :, None]

            # Components W X~
            A[:, :, n_C + idx_rbf_re] = -1 * weights[:, :, None] * rbf_real[None, :, :] * responses[:, :, None]
            A[:, :, n_C + idx_rbf_complex_re] = \
                -1 * weights[:, :, None] * rbf_complex_re[None, :, :] * responses[:, :, None]
            A[:, :, n_C + idx_rbf_complex_im] = \
                -1 * weights[:, :, None] * rbf_complex_im[None, :, :] * responses[:, :, None]
            if not preserve_dc:
                A[:, :, -1] = -1 * weights[:, :] * responses[:, :]

            # The numpy QR decomposition in mode 'r' will be A = Q R
            # size A=M, N then size R=min(M,N), N
            #
            # To get R22, we need to make sure that we get enough columns
            # so that R22 fits to the size of C~. This condition can always
            # be fulfilled because the N size of R22 is the same as for A.

            # The number of rows that we need (from the bottom of R) needs to be chosen such that
            # all components of C are multiplied with a zero of the lower triangle. So if we
            # have K rows, the second row has one zero, the third row has two zeros, effectively
            # removing the first and second component of C and so on.
            # Thus, the number of rows left until the bottom of R22 is reached is
            # n_rows_R22=K-n_C

            # QR decomposition. Note: The hstack is not actually stacking
            # "horizontally" but it's stacking along the second dimension.
            # The first dimension is responses, the second is freqs.
            # Thus, dimension two is doubled to 2*n_freqs by the stack.
            #
            # We could basically also run a half sized QR of the complex A
            # but this will yield also complex C_tilde because we will get a
            # complex R. So what we do is Re(A)x=0 && Im(A)x=0 because this
            # is the equivalent of Ax=0 doubling the number of equations.
            # Only then we get a real x (C_tilde) which fulfils both the real
            # and imaginary part equations. The complex Ax=0 would lead to
            # complex C_tilde and it would be impossible to convert it back to
            # real only because (a+jb)*(c+jd)=ac-bd+j(bc+ad) so all the parts
            # are mixed up between A and x in the solution.
            if preserve_dc:
                Q, R = np.linalg.qr(np.hstack((A.real, A.imag)), 'reduced')

                # Get R22
                R22 = R[:, n_C:, n_C:]

                # Get Q2. Q = [Q1 Q2] is shape n_responses, 2 * n_freqs, K
                Q2 = Q[:, :, n_C:]

                # Calculate RHS: TODO: What if we have no DC!!!
                H_DC = weights[:, 0] * responses[:, 0]
                H = weights * responses[:, :] - H_DC[:, None]
                H = np.hstack((np.real(H), np.imag(H)))
                H = np.expand_dims(H, 2)
                RHS = np.transpose(Q2, axes = (0, 2, 1)) @ H
            else:
                R = np.linalg.qr(np.hstack((A.real, A.imag)), 'r')

                # Get R22
                R22 = R[:, n_C:, n_C:]

        # Memory saver: Run in serial for each response. The code is essentially
        # the same as above. See comments above for comments on the code.
        else:
            for i in range(n_responses):
                A = np.empty((n_freqs, n_C + n_C_tilde), dtype=complex)

                # Components W X
                A[:, idx_rbf_re] = weights[i, :, None] * rbf_real[None, :, :]
                A[:, idx_rbf_complex_re] = weights[i, :, None] * rbf_complex_re[None, :, :]
                A[:, idx_rbf_complex_im] = weights[i, :, None] * rbf_complex_im[None, :, :]
                if not preserve_dc and fit_constant:
                    A[:, idx_const] = 1 * weights[i, :, None]
                if fit_proportional:
                    A[:, idx_prop] = weights[i, :, None] * s[None, :, None]

                # Components W X~
                A[:, n_C + idx_rbf_re] = -1 * weights[i, :, None] * rbf_real[None, :, :] * responses[i, :, None]
                A[:, n_C + idx_rbf_complex_re] = \
                    -1 * weights[i, :, None] * rbf_complex_re[None, :, :] * responses[i, :, None]
                A[:, n_C + idx_rbf_complex_im] = \
                    -1 * weights[i, :, None] * rbf_complex_im[None, :, :] * responses[i, :, None]
                if not preserve_dc:
                    A[:, -1] = -1 * weights[i, :] * responses[i, :]

                # QR decomposition. Note: Here we have to use vstack instead of hstack
                # to stack in the first dimension (freq).
                if preserve_dc:
                    R = np.linalg.qr(np.vstack((A.real, A.imag)), 'r')

                    # Get R22
                    R22[i] = R[n_C:, n_C:]

                    # Get Q2. Q = [Q1 Q2] is shape 2 * n_freqs, K
                    Q2 = Q[:, n_C:]

                    # Calculate RHS: TODO: What if we have no DC!!!
                    H = weights[i, :] * responses[i, :] - weights[i, 0] * responses[i, 0]
                    H = np.transpose(Q2, axes = (1, 0)) @ H
                    RHS[i] = np.hstack(np.real(H), np.imag(H))
                else:
                    R = np.linalg.qr(np.vstack((A.real, A.imag)), 'r')

                    # Get R22
                    R22[i] = R[n_C:, n_C:]

        # Build A_dense. This is the representation of the initial big system
        # matrix A with the sparsity and the unused C terms removed.
        if preserve_dc:
            A_dense = np.empty((n_responses * n_rows_R22, n_C_tilde))
            A_dense = R22.reshape((n_responses * n_rows_R22, n_C_tilde))

            # Build right hand side b_dense
            b_dense = np.empty(n_responses * n_rows_R22)
            b_dense = RHS.reshape((-1, 1))

            # Dummy
            d_tilde_norm = 1

        else:
            A_dense = np.empty((n_responses * n_rows_R22 + 1, n_C_tilde))
            A_dense[:-1, :] = R22.reshape((n_responses * n_rows_R22, n_C_tilde))

            # The extra equation is weighted such that its influence in the least
            # squares is equal to all other equations. In the original Gustavsen
            # VF-Relaxed paper, norm(H)/(n_responses*n_freqs) is used.
            weight_extra = np.linalg.norm(responses * weights) / np.size(responses)

            # Extra equation for d~
            A_dense[-1, idx_rbf_re] = weight_extra * np.sum(rbf_real.real, axis=0)
            A_dense[-1, idx_rbf_complex_re] = weight_extra * np.sum(rbf_complex_re.real, axis=0)
            A_dense[-1, idx_rbf_complex_im] = weight_extra * np.sum(rbf_complex_im.real, axis=0)
            A_dense[-1, -1] = weight_extra * n_freqs # Results from summing a 1 over n_freqs

            d_tilde_norm = np.linalg.norm(A_dense[:, :-1]) / \
                    (np.size(A_dense, axis = 0) * (np.size(A_dense, axis = 1) - 1))
            A_dense[:, -1] *= d_tilde_norm

            # Right hand side b_dense
            b_dense = np.zeros(n_responses * n_rows_R22 + 1)
            b_dense[-1] = weight_extra * n_samples# * d_tilde_norm

        return A_dense, b_dense, idx_rbf_re, idx_rbf_complex_re, idx_rbf_complex_im, d_tilde_norm

    def _get_C_tilde_and_d_tilde_from_C_tilde_modified(self, poles, C_tilde_modified):
        # Converts C_tilde_modified of the modified VF form to C_tilde of the original VF form

        # Calculate equivalents in stardard partial fraction form
        # Squeeze to get a row vector
        C_tilde = np.squeeze(np.copy(C_tilde_modified))
        d_tilde = 1
        for i, pole in enumerate(poles):
            if np.imag(pole) == 0:
                d_tilde += C_tilde[i]
                C_tilde[i] = C_tilde[i] * np.real(pole)
            else:
                d_tilde += 2 * C_tilde[i]
                C = C_tilde[i] + 1j * C_tilde[i + 1]
                C_tilde[i] = np.real(C * pole)
                C_tilde[i + 1] = np.imag(C * pole)

        return C_tilde, d_tilde

    def _calculate_new_poles(self, poles, C_tilde, d_tilde, idx_x_dense_re, idx_x_dense_complex_re, idx_x_dense_complex_im):
        # Calculates a new set of poles by calculating the eigenvalues of matrix H

        # Build H=A-BD^-1C^T
        H = np.zeros((len(C_tilde), len(C_tilde)))

        # Get indices of poles
        idx_poles_real, idx_poles_complex = self._get_indices_poles(poles)

        # Get real and complex poles
        poles_real = poles[idx_poles_real]
        poles_complex = poles[idx_poles_complex]

        # Build H
        H[idx_x_dense_re, idx_x_dense_re] = poles_real.real
        H[idx_x_dense_re] -= C_tilde / d_tilde
        H[idx_x_dense_complex_re, idx_x_dense_complex_re] = poles_complex.real
        H[idx_x_dense_complex_re, idx_x_dense_complex_im] = poles_complex.imag
        H[idx_x_dense_complex_im, idx_x_dense_complex_re] = -1 * poles_complex.imag
        H[idx_x_dense_complex_im, idx_x_dense_complex_im] = poles_complex.real
        H[idx_x_dense_complex_re] -= 2 * C_tilde / d_tilde

        # Compute eigenvalues of H. These are the new poles.
        poles_new = np.linalg.eigvals(H)

        # Replace poles for next iteration by new ones. For complex conjugate
        # pole pairs, only the pole with the positive imaginary part is saved.
        poles = poles_new[np.nonzero(poles_new.imag >= 0)]

        # Flip real part of unstable poles
        poles.real = -1 * np.abs(poles.real)

        return poles, d_tilde

    def _relocate_poles(self,
        poles, omega, responses, weights,
        fit_constant, fit_proportional, preserve_dc, memory_saver):

        # In general, we have one "big" system Ax=b, in which the solution
        # vector x contains all Ci and C~:
        #
        # [W1 X, 0,       ..., -W1 H1 X~ ] [C1]   [0]
        # [0,    W2 X, 0, ..., -W2 H2 X~ ] [C2]   [0]
        # [0, ...,       Wn X, -Wn Hn X~ ] [..] = [0]
        # [0, ...,                    J ] [C~]   [weight_extra*n_samples]

        # Notes on the solution vector x: We are only interested in the C~.

        # Notes on the result vector b: In VF-Relaxed, all equations
        # except the last will have b=0. In the original-VF, we would have
        # b=Hi for every row that has b=0 in VF-Relaxed. This is a direct
        # result of not setting d=1 in the sigma(s) in VF-Relaxed but instead
        # putting d~ into the solution vector and just enforcing a non-zero
        # solution for d~ by adding the extra equation represented by the last
        # row in the big system.

        # Notes on the last row: This represents eq. 8 in Gustavsen-Relaxed-VF.
        # J=weight_extra*sum(real(X)) size: 1 x (n_poles+1)
        # The summation goes over all n_freq points

        # Notes on the remaining rows:
        #
        # X := rational basis functions with size n_freqs x (n_poles+n_extra)
        #       Xkj := 1/(sk-aj) (for j=1..n_poles and for all k)
        # and   Xkj := 1 (for j=n_poles+1* and for all k) (only if fit_constant == True)
        # and   Xkj := s (for j=n_poles+2* and for all k) (only if fit_constant == True and fit_proportional == True)
        # and   Xkj := s (for j=n_poles+1* and for all k) (only if fit_constant == False and fit_proportional == True)

        # X~ := rational basis functions with size n_freqs x (n_poles+1)
        #       Xkj := 1/(sk-aj) (for j=1..n_poles and for all k)
        # and   Xkj := 1 (for j=n_poles+1 and for all k) (only if fit_constant == True)
        # The Y represent the rational basis functions of sigma(s) including
        # all the same poles as in X, but only one extra element for the d~ term,
        # which is always present. The e term does not go into sigma(s).

        # If fit_proportional == False and fit_constant == True, X==X~.
        # In all other cases, only the rational basis functions will be the
        # same for X and X~.

        #
        # Wi := weights: diag(wi1,...wi_n_freqs)
        #
        # Hi := response: diag(hi(s1), ..., hi(s_n_freqs)),
        #
        # Ci=[c1i, ..., cn_polesi, di (optional), ei(optional)]^T
        # C~=[c1~, ..., cn_poles~, d~]^T

        # The big system could directly be solved but it quickly becomes
        # pretty big, so some authors developed a method to reduce the
        # computational complexity, which is implemented in this method.
        # It is based on this publication:
        #
        # "Macromodeling of Multiport Systems Using a Fast Implementation of the Vector
        # Fitting Method", Dirk Deschrijver, Michal Mrozowski, Tom Dhaene, Daniel De Zutter,
        # IEEE MICROWAVE AND WIRELESS COMPONENTS LETTERS, VOL. 18, NO. 6, JUNE 2008.

        # Because the rows of A contain only zeros except for two
        # elements, the rows Ai of A can be written like this:

        # [Wi*X -Wi Hi X~] [Ci] = [0]
        #                  [C~]   [0]
        #
        # and the last row of A yields
        # [J] [C~] = [weight_extra*n_samples]
        #
        # All those rows still form one equation system that can't be treated
        # separately. But we can do a QR factorization of each of those
        # rows, except for the last one: Ai=Q [R11, R12; 0, R22]
        #
        # Because we are only interested in the C~ of the solution vector x,
        # and we have the 0 in the R matrix in the lower diagonal of R,
        # we can write the equations for C~:
        # R22*C~ = 0
        # This is possible because the C~ depend only on R22 but not
        # on the other elements of R.
        #
        # To account for all rows Ai representing the entire equation system,
        # we have to do a QR for each row and combine all the R22_i into
        # the final equation system that has now only the C~ in the solution vector:
        #
        # [R22_1               ]        [0]
        # [R22_2,              ]        [0]
        # [R22_3,              ] [C~] = [0]
        # [...,                ]        ...
        # [R22_(n_responses+1) ]        [0]
        # [J                   ]        [weight_extra*n_freqs]

        ##############
        # Further notes on the residual basis functions used in X and X~:
        # In general, for complex conjugate pole pairs, also the residues
        # are complex conjugate.
        #
        # After the first step of pole relocation described above is complete,
        # (i.e. the calculation of the C~), the second (and final) step
        # of pole relocation is done: We calculate the eigvals of another matrix,
        # and those eigvals are directly the poles.
        #
        # In general, this matrix is complex. We coud calculate its eigvals
        # directly from the complex matrix, but we have a constraint:

        # We know that for complex conjugate pole pairs, we get complex conjugate
        # eigenvalues, but the eigenvalues of a complex matrix are not necessarily
        # complex conjugate pairs. Only for real matrices we are guaranteed to get
        # complex conjugate pair eigenvalues.
        # This is a direct result of the
        # definition of eigenvalues as the roots of the characteristic polynomial.
        # If the coefficients of this polynomial are all real (which are the
        # elements of the matrix!), the roots will be complex conjugate pairs.
        #
        # And this is also what we need, because we will save only
        # one pole per pair and expect that the other pole of the pair
        # is exactly its conjugate.
        #
        # The reason why we want this is that later in the synthesis, we rely
        # on complex poles to occur in complex conjugate pairs. An arbitrary, single
        # complex pole can't be synthesized! Only a conjugate complex pole pair can.
        #
        # Because of this, we can't just calculate the eigenvalues in the last
        # step using a complex matrix (this would be simpler and faster),
        # but instead we have to transform the complex matrix into a real matrix
        # using a similarity transformation.
        #
        # In this transformation, a diagonal 2x2 block submatrix with complex
        # conjugate pairs will be transformed into a real matrix that has the
        # same eigenvalues:
        #
        # eigvals([a+jb, 0; 0, a-jb]) == eigvals([a, b; -b, a])
        #
        # a.k.a. complex diagonal form (cdf) vs. real diagonal form (rdf)
        #
        # The final matrix is not a diagonal matrix as in the example, but will also
        # contain off diagonal, non zero elements. The eigenvalue problem to solve
        # will be eigvals(A-BD^-1C~^T).

        # The final matrix comes from transforming sigma(s) into a state space model
        # in parallel/diagonal form: sigma(s)=C(sI-A)^-1 B + D
        # A := Diagonal state/system matrix A=diag(p1,...pn) containing the initial poles
        # B := Input matrix: A column vector of ones
        # C := Output matrix, which is a row vector with the C~
        # D := Feed through/feed forward matrix D.
        #
        # For the similarity transformation we need the exact real and imaginary parts
        # of all elements of the matrix A-BD^-1C~^T.
        # The real an imaginary parts of A (which contains the poles on the diagonal)
        # for complex conjugate initial poles are directly available from the
        # initial complex pole that is saved in the poles array.
        # B and D are real, so only for C~ we need to make sure to get the real
        # and imaginary parts of complex conjugate residues.
        #
        # Normally C~ contains C~[i] and C~[i+1]=C~[i]* for a complex conjugate residue.
        # so we could solve the least squares problem in step 1 and then postprocess
        # it for complex poles by calculating Real(C~[i]) and Imag(C~[i]) from the
        # complex C~[i] or we can alternatively modify the least squares problem
        # such that for complex conjugate pole pairs, we get the real part of
        # the complex conjugate pair in C~[i] and the imaginary part of the
        # complex conjugate pair in C~[i+1] by doing the following modification:
        #
        # Xkj     =     (1/(sk-aj)+1/(sk-aj*))
        # Xk(j+1) = j * (1/(sk-aj)-1/(sk-aj*))
        #
        # With this modification C~ is real and we can directly use it
        # to build the final real matrix for the eigenvalue calculation.

        # Get R22 equation system A_dense x_dense = b_dense
        # x_dense will contain the C_tilde. The idx_x_dense* are the indices of the
        # real and complex residues in C_tilde.
        A_dense, b_dense, idx_x_dense_re, idx_x_dense_complex_re, idx_x_dense_complex_im, d_tilde_norm = \
            self._get_R22_equation_system(
                responses, weights, poles, omega,
                fit_constant, fit_proportional,
                preserve_dc, memory_saver)

        # Condition number of the linear system
        cond_A_dense = np.linalg.cond(A_dense)

        # Solve least squares for C~
        C_tilde, residuals_A_dense, rank_A_dense, singular_values_A_dense = \
            np.linalg.lstsq(A_dense, b_dense, rcond=None)

        # Rank deficiency
        full_rank_A_dense = np.min(A_dense.shape)
        rank_deficiency_A_dense = full_rank_A_dense - rank_A_dense

        if preserve_dc:
            # Convert C_tilde_modified into C_tilde and d_tilde
            C_tilde, d_tilde = self._get_C_tilde_and_d_tilde_from_C_tilde_modified(poles, C_tilde)
        else:
            d_tilde = C_tilde[-1] * d_tilde_norm
            C_tilde = C_tilde[:-1]

        # Calculates a new set of poles by calculating the eigenvalues of matrix H
        poles, d_tilde = self._calculate_new_poles(
            poles, C_tilde, d_tilde, idx_x_dense_re, idx_x_dense_complex_re, idx_x_dense_complex_im)

        # Append convergence metrics to history
        self.d_tilde_history.append(d_tilde)
        self.history_cond_A_dense.append(cond_A_dense)
        self.history_rank_deficiency_A_dense.append(rank_deficiency_A_dense)

        # Maximum singular value of A_dense
        new_max_singular_value_A_dense = np.amax(singular_values_A_dense)

        # Calculate relative change of max_singular_value_A_dense
        delta_rel_max_singular_value_A_dense = np.abs(
            (new_max_singular_value_A_dense-self.max_singular_value_A_dense) / self.max_singular_value_A_dense)

        # Save new_max_singular_value_A_dense
        self.max_singular_value_A_dense = new_max_singular_value_A_dense

        self.delta_rel_max_singular_value_A_dense_history.append(delta_rel_max_singular_value_A_dense)

        logger.info(f'PoleRelocation: Cond = {cond_A_dense:.1e} RankDeficiency = {rank_deficiency_A_dense} '
                    f'dRelMaxSv = {delta_rel_max_singular_value_A_dense:.4e}')

        return poles, d_tilde

    def _fit_residues(self, poles, omega, responses, weights, fit_constant, fit_proportional, preserve_dc):
        n_responses, n_freqs = np.shape(responses)
        s = 1j * omega

        # Get total number of poles, counting complex conjugate pairs as 2 poles
        n_poles=np.sum((poles.imag != 0) + 1)

        # Get indices of poles
        idx_poles_real, idx_poles_complex = self._get_indices_poles(poles)

        # Initialize number of elements in C
        n_C = n_poles

        # Get index of constant term if we have it
        if not preserve_dc and fit_constant:
            idx_const = [n_C]
            n_C += 1

        # Get index of proportional term if we have it
        if fit_proportional:
            idx_prop = [n_C]
            n_C += 1

        # Get rational basis functions (RBF)
        rbf_real, rbf_complex_re, rbf_complex_im, idx_rbf_re, idx_rbf_complex_re, idx_rbf_complex_im = \
            self._get_rational_basis_functions(s, poles, preserve_dc)

        # Build matrix A
        A = np.empty((n_responses, n_freqs, n_C), dtype=complex)

        # Components W X
        A[:, :, idx_rbf_re] = weights[:, :, None] * rbf_real[None, :, :]
        A[:, :, idx_rbf_complex_re] = weights[:, :, None] * rbf_complex_re[None, :, :]
        A[:, :, idx_rbf_complex_im] = weights[:, :, None] * rbf_complex_im[None, :, :]

        if not preserve_dc and fit_constant:
            d_norm=np.empty(n_responses)
            d_norm[:]=np.asarray(
                [np.linalg.norm(A[i, :, :idx_const[0]-1]) / (n_freqs*(idx_const[0])) for i in range(n_responses)])
            A[:, :, idx_const] = 1 * d_norm[:, None, None] * weights[:, :, None]

        if fit_proportional:
            d_norm = np.empty(n_responses)
            d_norm[:] = np.asarray(
                [np.linalg.norm(A[i, :, :idx_prop[0] - 1]) / (n_freqs*(idx_prop[0])) for i in range(n_responses)])

            e_norm = d_norm / (np.linalg.norm(s) / n_freqs)
            A[:, :, idx_prop] = e_norm[:, None, None] * weights[:, :, None] * s[None, :, None]

        # Build responses_weigthed
        if preserve_dc:
            responses_weighted_dc = responses[:, 0] * weights[:, 0]
            responses_weighted = responses * weights - responses_weighted_dc[:, None]
        else:
            responses_weighted = responses * weights

        # Solve for C with least squares for every response
        x = np.empty((n_responses, n_C))
        for i in range(n_responses):
            Ai = np.vstack((A[i, :, :].real, A[i, :, :].imag))
            bi = np.hstack((responses_weighted[i, :].real, responses_weighted[i].imag)).T

            # Solve least squares and obtain results as stack of real part vector and imaginary part vector
            xi, residuals, rank, singular_values = np.linalg.lstsq(Ai, bi, rcond=None)

            # Append solution vector to x
            x[i] = xi

        # Residues holds the residues in standard partial fraction form
        residues = np.empty((len(responses), len(poles)), dtype=complex)

        if preserve_dc:
            residues[:, idx_poles_real] = x[:, idx_rbf_re] * np.real(poles[idx_poles_real])
            residues[:, idx_poles_complex] = \
                (x[:, idx_rbf_complex_re] + 1j * x[:, idx_rbf_complex_im]) * poles[idx_poles_complex]
        else:
            residues[:, idx_poles_real] = x[:, idx_rbf_re]
            residues[:, idx_poles_complex] = x[:, idx_rbf_complex_re] + 1j * x[:, idx_rbf_complex_im]

        # Constant
        if preserve_dc:
            # Constant in standard partial fraction form
            constant = np.real(responses[:, 0]) + \
                np.sum(np.real(x[:, idx_rbf_re]), axis = 1) + \
                2 * np.sum(np.real(x[:, idx_rbf_complex_re]), axis = 1)

        elif not preserve_dc and fit_constant:
            # Not preserve_dc and fit_constant
            constant = np.matrix.flatten(x[:, idx_const]) * d_norm

        else:
            # Otherwise constant is zero
            constant = np.zeros(n_responses)

        # Proportional
        if fit_proportional:
            proportional = np.matrix.flatten(x[:, idx_prop]) * e_norm
        else:
            proportional = np.zeros(n_responses)

        return residues, constant, proportional

    @staticmethod
    def _get_delta(poles, residues, constant, proportional, omega, responses, weights):
        s = 1j * omega

        # Initialize model with zeros
        model=np.zeros(np.shape(responses), dtype=complex)

        # Constant and proportional terms
        model += constant[:, None] + proportional[:, None] * s

        # Poles
        for i, pole in enumerate(poles):
            if np.imag(pole) == 0.0:
                # Real residue/pole
                model += residues[:, i, None] / (s - pole)
            else:
                # Complex conjugate residue/pole pair
                model += (residues[:, i, None] / (s - pole) +
                          np.conjugate(residues[:, i, None]) / (s - np.conjugate(pole)))

        # Weighted absolute error
        delta = np.abs(model - responses) * weights

        # Global maximum at each frequency across all individual responses
        return np.max(delta, axis=0)

    @staticmethod
    def _get_pole_candidates(delta, omega):
        # Determines new pole candidates. The delta is split into frequency bands
        # for which delta > mean(delta) and the maximum in each of those bands
        # are the candidates.

        # Subtract mean from delta
        delta = delta - np.mean(delta)

        # Stores the maximum of each band
        delta_max_in_bands=[]

        # Stores the index of the maximum of each band
        index_of_delta_max_in_bands=[]

        # Find the maximum in each band
        delta_max_in_current_band=0
        index_of_delta_max_in_current_band=0
        is_inside_of_band=False
        for i in range(len(delta)):
            # Outside_of_band
            if delta[i] < 0:
                # Transition from inside_of_band to outside_of_band
                if is_inside_of_band:
                    # Store maximum and its index
                    delta_max_in_bands.append(delta_max_in_current_band)
                    index_of_delta_max_in_bands.append(index_of_delta_max_in_current_band)

                    # Reset for next band
                    is_inside_of_band=False
            # Inside_of_band
            else:
                if is_inside_of_band:
                    # Check if we have a new maximum
                    if delta[i] >= delta_max_in_current_band:
                        # Save new maximum and its index for current band
                        delta_max_in_current_band=delta[i]
                        index_of_delta_max_in_current_band=i
                else:
                    is_inside_of_band=True
                    delta_max_in_current_band=delta[i]
                    index_of_delta_max_in_current_band=i

        # Process potential last band
        if is_inside_of_band:
            # Store maximum and its index
            delta_max_in_bands.append(delta_max_in_current_band)
            index_of_delta_max_in_bands.append(index_of_delta_max_in_current_band)

        # Convert lists to array
        delta_max_in_bands = np.array(delta_max_in_bands)
        index_of_delta_max_in_bands = np.array(index_of_delta_max_in_bands)

        # Plot for debug
        # import matplotlib.pyplot as plt
        # plt.plot(delta)
        # plt.plot(index_of_delta_max_in_bands, delta[index_of_delta_max_in_bands], "x")
        # plt.plot(np.zeros_like(delta), "--", color="gray")
        # plt.show()

        # Sort delta_max_in_bands and get indices from sort
        index_sorted_delta_max_in_bands = np.flip(np.argsort(delta_max_in_bands))

        # Create pole candidate with the omega corresponding to the obtained indices
        pole_candidates=np.array((-0.01 + 1j) * omega[index_sorted_delta_max_in_bands])

        return pole_candidates

    def get_rms_error(self, i=-1, j=-1, parameter_type: str = 's'):
        return self.get_total_abs_error(i, j, parameter_type)

    def get_total_abs_error(self, i=-1, j=-1, parameter_type: str = 's'):
        r"""
        Returns the root-mean-square (rms) error magnitude of the fit, i.e.
        :math:`\sqrt{ \mathrm{mean}(|S - S_\mathrm{fit} |^2) }`,
        either for an individual response :math:`S_{i+1,j+1}` or for larger slices of the network.

        Parameters
        ----------
        i : int, optional
            Row indices of the responses to be evaluated. Either a single row selected by an integer
            :math:`i \in [0, N_\mathrm{ports}-1]`, or multiple rows selected by a list of integers, or all rows
            selected by :math:`i = -1` (*default*).

        j : int, optional
            Column indices of the responses to be evaluated. Either a single column selected by an integer
            :math:`j \in [0, N_\mathrm{ports}-1]`, or multiple columns selected by a list of integers, or all columns
            selected by :math:`j = -1` (*default*).

        parameter_type: str, optional
            Representation type of the fitted frequency responses. Either *scattering* (:attr:`s` or :attr:`S`),
            *impedance* (:attr:`z` or :attr:`Z`) or *admittance* (:attr:`y` or :attr:`Y`).

        Returns
        -------
        rms_error : ndarray
            The rms error magnitude between the vector fitted model and the original network data.

        Raises
        ------
        ValueError
            If the specified parameter representation type is not :attr:`s`, :attr:`z`, nor :attr:`y`.
        """

        if i == -1:
            list_i = range(self.network.nports)
        elif isinstance(i, int):
            list_i = [i]
        else:
            list_i = i

        if j == -1:
            list_j = range(self.network.nports)
        elif isinstance(j, int):
            list_j = [j]
        else:
            list_j = j

        # Get network responses
        nw_responses = self._get_responses_from_network(parameter_type)

        error_mean_squared = 0
        for i in list_i:
            for j in list_j:
                nw_ij = nw_responses[:, i, j]
                fit_ij = self.get_model_response(i, j, self.network.f)
                error_mean_squared += np.mean(np.square(np.abs(nw_ij - fit_ij)))

        return np.sqrt(error_mean_squared / (len(list_i) * len(list_j)))

    def get_total_rel_error(self, i=-1, j=-1, parameter_type: str = 's'):
        r"""
        Returns the weighted root-mean-square (rms) error magnitude of the fit, i.e.
        :math:`\sqrt{ \mathrm{mean}(|S - S_\mathrm{fit} |^2) }`,
        either for an individual response :math:`S_{i+1,j+1}` or for larger slices of the network.

        Parameters
        ----------
        i : int, optional
            Row indices of the responses to be evaluated. Either a single row selected by an integer
            :math:`i \in [0, N_\mathrm{ports}-1]`, or multiple rows selected by a list of integers, or all rows
            selected by :math:`i = -1` (*default*).

        j : int, optional
            Column indices of the responses to be evaluated. Either a single column selected by an integer
            :math:`j \in [0, N_\mathrm{ports}-1]`, or multiple columns selected by a list of integers, or all columns
            selected by :math:`j = -1` (*default*).

        parameter_type: str, optional
            Representation type of the fitted frequency responses. Either *scattering* (:attr:`s` or :attr:`S`),
            *impedance* (:attr:`z` or :attr:`Z`) or *admittance* (:attr:`y` or :attr:`Y`).

        Returns
        -------
        rms_error : ndarray
            The rms error magnitude between the vector fitted model and the original network data.

        Raises
        ------
        ValueError
            If the specified parameter representation type is not :attr:`s`, :attr:`z`, nor :attr:`y`.
        """

        if i == -1:
            list_i = range(self.network.nports)
        elif isinstance(i, int):
            list_i = [i]
        else:
            list_i = i

        if j == -1:
            list_j = range(self.network.nports)
        elif isinstance(j, int):
            list_j = [j]
        else:
            list_j = j

        # Get network responses
        nw_responses = self._get_responses_from_network(parameter_type)

        error_mean_squared = 0
        for i in list_i:
            for j in list_j:
                nw_ij = nw_responses[:, i, j]
                fit_ij = self.get_model_response(i, j, self.network.f)
                error_mean_squared += np.mean(np.square(np.abs(nw_ij - fit_ij)/np.abs(nw_ij)))

        return np.sqrt(error_mean_squared / (len(list_i) * len(list_j)))

    def get_abs_error_vs_responses(self, parameter_type: str = 's'):
        r"""
        Returns the root-mean-square (rms) error magnitude of the fit, i.e.
        :math:`\sqrt{ \mathrm{mean}(|S - S_\mathrm{fit} |^2) }`,

        Parameters
        ----------
        parameter_type: str, optional
            Representation type of the fitted frequency responses. Either *scattering* (:attr:`s` or :attr:`S`),
            *impedance* (:attr:`z` or :attr:`Z`) or *admittance* (:attr:`y` or :attr:`Y`).

        Returns
        -------
        rms_error : ndarray
            The rms error magnitude between the vector fitted model and the original network data.

        Raises
        ------
        ValueError
            If the specified parameter representation type is not :attr:`s`, :attr:`z`, nor :attr:`y`.
        """

        # Get network responses
        nw_responses = self._get_responses_from_network(parameter_type)

        n_responses=np.size(nw_responses, axis=1)
        error_mean_squared = np.zeros((n_responses, n_responses))
        for i in range(n_responses):
            for j in range(n_responses):
                nw_ij = nw_responses[:, i, j]
                fit_ij = self.get_model_response(i, j, self.network.f)
                error_mean_squared[i, j] = np.sqrt(np.mean(np.square(np.abs(nw_ij - fit_ij))))

        return error_mean_squared

    def get_rel_error_vs_responses(self, parameter_type: str = 's'):
        r"""
        Returns the weighted root-mean-square (rms) error magnitude of the fit, i.e.
        :math:`\sqrt{ \mathrm{mean}(|S - S_\mathrm{fit} |^2) }`,

        Parameters
        ----------
        parameter_type: str, optional
            Representation type of the fitted frequency responses. Either *scattering* (:attr:`s` or :attr:`S`),
            *impedance* (:attr:`z` or :attr:`Z`) or *admittance* (:attr:`y` or :attr:`Y`).

        Returns
        -------
        rms_error : ndarray
            The rms error magnitude between the vector fitted model and the original network data.

        Raises
        ------
        ValueError
            If the specified parameter representation type is not :attr:`s`, :attr:`z`, nor :attr:`y`.
        """

        # Get network responses
        nw_responses = self._get_responses_from_network(parameter_type)

        n_responses=np.size(nw_responses, axis=1)
        error_mean_squared = np.zeros((n_responses, n_responses))
        for i in range(n_responses):
            for j in range(n_responses):
                nw_ij = nw_responses[:, i, j]
                fit_ij = self.get_model_response(i, j, self.network.f)
                error_mean_squared[i, j] = np.sqrt(np.mean(np.square(np.abs(nw_ij - fit_ij)/np.abs(nw_ij))))

        return error_mean_squared

    def get_abs_error(self, i: int = -1, j: int = -1, parameter_type: str = 's'):
        r"""
        Returns the absolute error magnitude of the fit

        Parameters
        ----------
        i, j : int, optional
            Row and column index of the response. If both are set to a value >= 0
            only the results for this response is returned. Otherwise the results
            for all responses are returned

        parameter_type: str, optional
            Representation type of the fitted frequency responses. Either *scattering* (:attr:`s` or :attr:`S`),
            *impedance* (:attr:`z` or :attr:`Z`) or *admittance* (:attr:`y` or :attr:`Y`).

        Returns
        -------
        error : ndarray
            The error magnitude between the vector fitted model and the original network data.

        Raises
        ------
        ValueError
            If the specified parameter representation type is not :attr:`s`, :attr:`z`, nor :attr:`y`.
        """

        # Get network responses
        nw_responses = self._get_responses_from_network(parameter_type)

        n_responses=np.size(nw_responses, axis=1)
        n_freqs=np.size(nw_responses, axis=0)

        if i >= 0 and j >= 0:
            nw_ij = nw_responses[:, i, j]
            fit_ij = self.get_model_response(i, j, self.network.f)
            error = np.abs(nw_ij - fit_ij)

        else:
            error = np.empty((n_responses, n_responses, n_freqs))
            for i in range(n_responses):
                for j in range(n_responses):
                    nw_ij = nw_responses[:, i, j]
                    fit_ij = self.get_model_response(i, j, self.network.f)
                    error[i, j, :] = np.abs(nw_ij - fit_ij)

        return error

    def get_rel_error(self, i: int = -1, j: int = -1, parameter_type: str = 's'):
        r"""
        Returns the relative error magnitude of the fit

        Parameters
        ----------
        i, j : int, optional
            Row and column index of the response. If both are set to a value >= 0
            only the results for this response is returned. Otherwise the results
            for all responses are returned

        parameter_type: str, optional
            Representation type of the fitted frequency responses. Either *scattering* (:attr:`s` or :attr:`S`),
            *impedance* (:attr:`z` or :attr:`Z`) or *admittance* (:attr:`y` or :attr:`Y`).

        Returns
        -------
        error : ndarray
            The error magnitude between the vector fitted model and the original network data.

        Raises
        ------
        ValueError
            If the specified parameter representation type is not :attr:`s`, :attr:`z`, nor :attr:`y`.
        """

        # Get network responses
        nw_responses = self._get_responses_from_network(parameter_type)

        n_responses=np.size(nw_responses, axis=1)
        n_freqs=np.size(nw_responses, axis=0)

        if i >= 0 and j >= 0:
            nw_ij = nw_responses[:, i, j]
            fit_ij = self.get_model_response(i, j, self.network.f)
            error = np.abs(nw_ij - fit_ij) / np.abs(nw_ij)

        else:
            error = np.empty((n_responses, n_responses, n_freqs))
            for i in range(n_responses):
                for j in range(n_responses):
                    nw_ij = nw_responses[:, i, j]
                    fit_ij = self.get_model_response(i, j, self.network.f)
                    error[i, j, :] = np.abs(nw_ij - fit_ij) / np.abs(nw_ij)

        return error


    def _get_indices_poles(self, poles):
        # Returns indices of real and complex conjugate pole pairs in poles

        # Get indices of real poles
        idx_poles_real = np.nonzero(poles.imag == 0)[0]

        # Get indices of complex poles
        idx_poles_complex = np.nonzero(poles.imag != 0)[0]

        return idx_poles_real, idx_poles_complex

    def _get_residues_and_constant_modified(self, poles, residues, constant):
        # Returns the residues_modified and constant_modified matching to the modified
        # vector fitting using basis functions r*s/(s-p)

        # Get indices of poles
        idx_poles_real, idx_poles_complex = self._get_indices_poles(poles)

        # Initialize empty
        residues_modified = np.empty(np.shape(residues), dtype=complex)
        constant_modified = np.empty(np.shape(constant), dtype=complex)

        # Residues in modified vf form
        residues_modified[:, idx_poles_real] = residues[:, idx_poles_real] / np.real(poles[idx_poles_real])
        residues_modified[:, idx_poles_complex] =  residues[:, idx_poles_complex] / poles[idx_poles_complex]

        # Constant in standard partial fraction form
        constant_modified = constant - \
            np.sum(np.real(residues_modified[:, idx_poles_real]), axis = 1) - \
            2 * np.sum(np.real(residues_modified[:, idx_poles_complex]), axis = 1)

        return residues_modified, constant_modified

    def _get_state_space_ABCDE(self, create_views = False,
                   ) -> tuple[np.ndarray, np.ndarray, np.ndarray, np.ndarray, np.ndarray]:
        """
        Private method.
        Returns the real-valued system matrices of the state-space representation of the current rational model, as
        defined in [#]_.

        Returns
        -------
        A : ndarray
            State-space matrix A holding the poles on the diagonal as real values with imaginary parts on the sub-
            diagonal
        B : ndarray
            State-space matrix B holding coefficients (1, 2, or 0), depending on the respective type of pole in A
        C : ndarray
            State-space matrix C holding the residues
        D : ndarray
            State-space matrix D holding the constants
        E : ndarray
            State-space matrix E holding the proportional coefficients (usually 0 in case of fitted S-parameters)

        Raises
        ------
        ValueError
            If the model parameters have not been initialized (by running :func:`vector_fit()` or :func:`read_npz()`).

        References
        ----------
        .. [#] B. Gustavsen and A. Semlyen, "Fast Passivity Assessment for S-Parameter Rational Models Via a Half-Size
            Test Matrix," in IEEE Transactions on Microwave Theory and Techniques, vol. 56, no. 12, pp. 2701-2708,
            Dec. 2008, DOI: 10.1109/TMTT.2008.2007319.
        """

        # Initial checks
        if self.poles is None:
            raise ValueError('poles = None; nothing to do. You need to run vector_fit() first.')
        if self.residues is None:
            raise ValueError('self.residues = None; nothing to do. You need to run vector_fit() first.')
        if self.proportional is None:
            raise ValueError('self.proportional = None; nothing to do. You need to run vector_fit() first.')
        if self.constant is None:
            raise ValueError('self.constant = None; nothing to do. You need to run vector_fit() first.')

        # Build A, B, C, D and E for the entire system including all pole groups

        # Get total number of ports including all pole groups
        n_ports = self._get_n_ports()

        # Get n_responses
        n_responses = n_ports * n_ports

        # These views enable easy accesss to the submatrices
        A_view = [x[:] for x in [[None] * n_ports] * n_ports]
        B_view = [x[:] for x in [[None] * n_ports] * n_ports]
        C_view = [x[:] for x in [[None] * n_ports] * n_ports]

        # Get model orders for every pole group
        model_orders=np.array([self.get_model_order(x) for x in range(len(self.poles))])

        # For every big column of C we need to find the number of subcolumns
        n_subcolumns_in_columns_of_C = np.empty((n_ports))
        # Working column wise: j'th column:
        for j in range(n_ports):
            # Get indices of the responses of the first column S11, S21, S31, ...
            indices_responses = j + np.arange(0, n_responses, n_ports)
            # Get pole group of every response
            indices_pole_groups = self.map_idx_response_to_idx_pole_group[indices_responses]
            # Get sorted unique pole groups
            sorted_unique_indices_pole_groups = np.unique(indices_pole_groups)
            # Get total model order for column
            model_order_column = int(np.sum(model_orders[sorted_unique_indices_pole_groups]))
            # Save
            n_subcolumns_in_columns_of_C[j] = model_order_column

        # Create empty output matrices
        n_A = int(np.sum(n_subcolumns_in_columns_of_C))
        A = np.zeros(shape=(n_A, n_A))
        B = np.zeros(shape=(n_A, n_ports))
        C = np.zeros(shape=(n_ports, n_A))
        D = np.zeros(shape=(n_ports, n_ports))
        E = np.zeros(shape=(n_ports, n_ports))

        # Index on diagonal of A
        idx_diag_A = 0
        # Column offset of the columns of C
        offset_col_C = 0
        # Working column wise for every big column j of C:
        for j in range(n_ports):
            # Get indices of the responses of the first column S11, S21, S31, ...
            indices_responses = j + np.arange(0, n_responses, n_ports)
            # Get pole group of every response
            indices_pole_groups = self.map_idx_response_to_idx_pole_group[indices_responses]
            # Get sorted unique pole groups
            sorted_unique_indices_pole_groups = np.unique(indices_pole_groups)
            # Get number of poles for every unique pole group
            model_orders_per_group = [self.get_model_order(x) for x in sorted_unique_indices_pole_groups]
            # Get total model order for column
            model_order_column = np.sum(model_orders_per_group)
            # Create dict mapping sorted_unique_indices_pole_groups to i (row index)
            map_sorted_unique_indices_pole_groups_to_i = \
                {x: np.nonzero(indices_pole_groups == x)[0] for x in sorted_unique_indices_pole_groups}

            # Work pole-group-wise
            for idx_pole_group in sorted_unique_indices_pole_groups:
                poles = self.poles[idx_pole_group]
                residues = self.residues[idx_pole_group]
                constant = self.constant[idx_pole_group]
                proportional = self.proportional[idx_pole_group]

                # Create contribution of this pole group into A and B
                for pole in poles:
                    if np.imag(pole) == 0.0:
                        # Real pole
                        A[idx_diag_A, idx_diag_A] = np.real(pole)
                        B[idx_diag_A, j] = 1
                        idx_diag_A += 1
                    else:
                        # Complex-conjugate pole
                        A[idx_diag_A, idx_diag_A] = np.real(pole)
                        A[idx_diag_A, idx_diag_A + 1] = np.imag(pole)
                        A[idx_diag_A + 1, idx_diag_A] = -1 * np.imag(pole)
                        A[idx_diag_A + 1, idx_diag_A + 1] = np.real(pole)
                        B[idx_diag_A, j] = 2
                        idx_diag_A += 2

                # Process all responses that are part of this pole group
                for i in map_sorted_unique_indices_pole_groups_to_i[idx_pole_group]:
                    # Get idx_response
                    idx_response = i * n_ports + j
                    idx_pole_group_member=self.map_idx_response_to_idx_pole_group_member[idx_response]

                    # Initialize idx_col_C to offset_col_C
                    idx_col_C = offset_col_C
                    for residue in residues[idx_pole_group_member]:
                        if np.imag(residue) == 0.0:
                            C[i, idx_col_C] = np.real(residue)
                            idx_col_C += 1
                        else:
                            C[i, idx_col_C] = np.real(residue)
                            C[i, idx_col_C + 1] = np.imag(residue)
                            idx_col_C += 2

                    if create_views:
                        # Create view on C
                        C_view[i][j] = C[i, offset_col_C:idx_col_C]

                        # Create view on A
                        A_view[i][j] = A[offset_col_C:idx_col_C, offset_col_C:idx_col_C]

                        # Create view on B
                        B_view[i][j] = np.expand_dims(B[offset_col_C:idx_col_C, j], 1)

                    # Create D and E
                    D[i, j] = constant[idx_pole_group_member]
                    E[i, j] = proportional[idx_pole_group_member]

                # Increment offset for next pole group
                offset_col_C = idx_col_C

        if create_views:
            return A, B, C, D, E, A_view, B_view, C_view
        else:
            return A, B, C, D, E

    def _get_state_space_FABCDE(self, s, create_views = False, create_modified = False):
        # Creates the state space matrix F=(sI-A)^-1 B and C, D, E

        # Input argument s is an array of complex frequencies s = 1j * omega for which F is built.

        # In the calculation of F no direct inversion is used and the diagonality properties are
        # used to efficiently invert it. Also the matrix multiplication with B is efficiently calculated
        # as an element wise multiplication instead. This is possible because also the inverted matrix is diagonal.

        # Debug: Use this code to check if views work as expected:
        # It should print OK for every response
        #
        # freqs = np.linspace(np.min(vf.network.f), np.max(vf.network.f), 201)
        # s_eval = 2j * np.pi * freqs
        # F, F_modified, A, B, C, C_modified, D, D_modified, E, \
        #     F_view, F_modified_view, A_view, B_view, C_view, C_modified_view, \
        #     nC, C_col_idx_begin, C_col_idx_end = \
        #     vf._get_state_space_FABCDE(s_eval, create_views = True, create_modified = True)
        # S_modified = C_modified @ F_modified + D_modified
        # S = C @ F + D
        # for i in range(n_ports):
        #     for j in range(n_ports):
        #         Sij_modified = np.expand_dims(C_modified_view[i][j], 0) @ F_modified_view[i][j].T + D_modified[i, j]
        #         Sij = np.expand_dims(C_view[i][j], 0) @ F_view[i][j].T + D[i, j]
        #         if np.allclose(Sij_modified, S_modified[:, i, j]) \
        #             and np.allclose(Sij, S[:, i, j])\
        #             and np.allclose(Sij, Sij_modified):
        #             print(f'OK {i} {j}')
        #
        # End of debug code

        # Get total number of ports including all pole groups
        n_ports = self._get_n_ports()

        # Get number of frequencies
        n_freqs = np.size(s, axis = 0)

        # Get n_responses
        n_responses = n_ports * n_ports

        # These views enable easy accesss to the submatrices
        F_view = [x[:] for x in [[None] * n_ports] * n_ports]
        A_view = [x[:] for x in [[None] * n_ports] * n_ports]
        B_view = [x[:] for x in [[None] * n_ports] * n_ports]
        C_view = [x[:] for x in [[None] * n_ports] * n_ports]
        C_col_idx_begin = [x[:] for x in [[None] * n_ports] * n_ports]
        C_col_idx_end = [x[:] for x in [[None] * n_ports] * n_ports]

        # Number of elements in each C_view[i, j]
        nC = np.zeros(shape=(n_ports, n_ports))

        # Get model orders for every pole group
        model_orders=np.array([self.get_model_order(x) for x in range(len(self.poles))])

        # For every big column of C we need to find the number of subcolumns
        n_subcolumns_in_columns_of_C = np.empty((n_ports))
        # Working column wise: j'th column:
        for j in range(n_ports):
            # Get indices of the responses of the first column S11, S21, S31, ...
            indices_responses = j + np.arange(0, n_responses, n_ports)
            # Get pole group of every response
            indices_pole_groups = self.map_idx_response_to_idx_pole_group[indices_responses]
            # Get sorted unique pole groups
            sorted_unique_indices_pole_groups = np.unique(indices_pole_groups)
            # Get total model order for column
            model_order_column = int(np.sum(model_orders[sorted_unique_indices_pole_groups]))
            # Save
            n_subcolumns_in_columns_of_C[j] = model_order_column

        # Create empty output matrices
        n_A = int(np.sum(n_subcolumns_in_columns_of_C))
        F = np.zeros(shape=(n_freqs, n_A, n_ports), dtype = complex)
        A = np.zeros(shape=(n_A, n_A))
        B = np.zeros(shape=(n_A, n_ports))
        C = np.zeros(shape=(n_ports, n_A))
        D = np.zeros(shape=(n_ports, n_ports))
        E = np.zeros(shape=(n_ports, n_ports))

        # Index on diagonal of A
        idx_diag_A = 0
        # Column offset of the columns of C
        offset_col_C = 0
        # Working column wise for every big column j of C:
        for j in range(n_ports):
            # Get indices of the responses of the first column S11, S21, S31, ...
            indices_responses = j + np.arange(0, n_responses, n_ports)
            # Get pole group of every response
            indices_pole_groups = self.map_idx_response_to_idx_pole_group[indices_responses]
            # Get sorted unique pole groups
            sorted_unique_indices_pole_groups = np.unique(indices_pole_groups)
            # Get number of poles for every unique pole group
            model_orders_per_group = [self.get_model_order(x) for x in sorted_unique_indices_pole_groups]
            # Get total model order for column
            model_order_column = np.sum(model_orders_per_group)
            # Create dict mapping sorted_unique_indices_pole_groups to i (row index)
            map_sorted_unique_indices_pole_groups_to_i = \
                {x: np.nonzero(indices_pole_groups == x)[0] for x in sorted_unique_indices_pole_groups}

            # Work pole-group-wise
            for idx_pole_group in sorted_unique_indices_pole_groups:
                poles = self.poles[idx_pole_group]
                residues = self.residues[idx_pole_group]
                constant = self.constant[idx_pole_group]
                proportional = self.proportional[idx_pole_group]
                if create_modified:
                    residues, constant = \
                        self._get_residues_and_constant_modified(poles, residues, constant)

                # Create contribution of this pole group into A and B
                for pole in poles:
                    if np.imag(pole) == 0.0:
                        # Real pole
                        if create_modified:
                            F[:, idx_diag_A, j] = s / (s - np.real(pole))
                        else:
                            F[:, idx_diag_A, j] = 1 / (s - np.real(pole))
                        A[idx_diag_A, idx_diag_A] = np.real(pole)
                        B[idx_diag_A, j] = 1
                        idx_diag_A += 1
                    else:
                        # Complex-conjugate pole
                        denom = (s - np.real(pole))**2 + np.imag(pole)**2
                        if create_modified:
                            F[:, idx_diag_A, j] = s * 2 * (s - np.real(pole)) / denom
                            F[:, idx_diag_A + 1, j] = s * -2 * np.imag(pole) / denom
                        else:
                            F[:, idx_diag_A, j] = 2 * (s - np.real(pole)) / denom
                            F[:, idx_diag_A + 1, j] = -2 * np.imag(pole) / denom
                        A[idx_diag_A, idx_diag_A] = np.real(pole)
                        A[idx_diag_A, idx_diag_A + 1] = np.imag(pole)
                        A[idx_diag_A + 1, idx_diag_A] = -1 * np.imag(pole)
                        A[idx_diag_A + 1, idx_diag_A + 1] = np.real(pole)
                        B[idx_diag_A, j] = 2
                        idx_diag_A += 2

                # Process all responses that are part of this pole group
                for i in map_sorted_unique_indices_pole_groups_to_i[idx_pole_group]:
                    # Get idx_response
                    idx_response = i * n_ports + j
                    idx_pole_group_member=self.map_idx_response_to_idx_pole_group_member[idx_response]

                    # Initialize idx_col_C to offset_col_C
                    idx_col_C = offset_col_C
                    for idx_residue in range(len(residues[idx_pole_group_member])):
                        residue = residues[idx_pole_group_member][idx_residue]
                        if np.imag(residue) == 0.0:
                            C[i, idx_col_C] = np.real(residue)
                            idx_col_C += 1
                        else:
                            C[i, idx_col_C] = np.real(residue)
                            C[i, idx_col_C + 1] = np.imag(residue)
                            idx_col_C += 2

                    if create_views:
                        # Create view on A
                        A_view[i][j] = A[offset_col_C:idx_col_C, offset_col_C:idx_col_C]

                        # Create view on B
                        B_view[i][j] = np.expand_dims(B[offset_col_C:idx_col_C, j], 1)

                        # Create view on C
                        C_view[i][j] = C[i, offset_col_C:idx_col_C]

                        # Create view on F
                        F_view[i][j] = F[:, offset_col_C:idx_col_C, j]

                        # Create nC
                        nC[i, j] = idx_col_C - offset_col_C

                        # Create C columns index ranges
                        C_col_idx_begin[i][j] = offset_col_C
                        C_col_idx_end[i][j] = idx_col_C

                    # Create D and E
                    D[i, j] = constant[idx_pole_group_member]
                    E[i, j] = proportional[idx_pole_group_member]

                # Increment offset for next pole group
                offset_col_C = idx_col_C

        if create_views:
            return F, A, B, C, D, E, \
                F_view, A_view, B_view, C_view, \
                nC, C_col_idx_begin, C_col_idx_end

        else:
            return F, A, B, C, D, E

    @staticmethod
    def _get_S_from_state_space_ABCDE(s: np.ndarray,
                          A: np.ndarray, B: np.ndarray, C: np.ndarray, D: np.ndarray, E: np.ndarray) -> np.ndarray:
        # Returns S-Parameters calculated from state space matrices A, B, C, D, E
        n_A = np.size(A, axis = 0)

        # Get total number of ports including all pole groups
        n_ports = np.size(B, axis = 1)

        # Get number of frequencies
        n_freqs = np.size(s, axis = 0)

        # Initialize F = (sI - A)^-1 B
        F = np.zeros(shape=(n_freqs, n_A, n_ports), dtype = complex)

        # Iterate over diagonal of A
        idx_diag_A = 0
        while True:
            # Get real and potential imaginary part
            real_pole = A[idx_diag_A, idx_diag_A]
            imag_pole = A[idx_diag_A, idx_diag_A + 1]
            # Check if it is a real 1x1 block or a complex conjugate 2x2 block submatrix
            if imag_pole == 0:
                # Real 1x1 block
                F[:, idx_diag_A, :] = (1 / (s - real_pole)) * B[None, idx_diag_A, :]
                idx_diag_A += 1
            else:
                # Complex-conjugate pole
                denom = (s - real_pole)**2 + imag_pole**2
                F[:, idx_diag_A, :] = ((s - real_pole) / denom) * B[None, idx_diag_A, :]
                F[:, idx_diag_A + 1, :] = (-1 * imag_pole / denom) * B[None, idx_diag_A, :]
                idx_diag_A += 2

            # Stop if we don't have at least 2 elements left on the diagonal
            if idx_diag_A > n_A - 2:
                break

        # Handle potential singnle element left on the diagonal
        if idx_diag_A != n_A:
            # Get real part
            real_pole = A[idx_diag_A, idx_diag_A]
            # Real 1x1 block
            F[:, idx_diag_A, :] = (1 / (s - real_pole)) * B[None, idx_diag_A, :]

        # Calculate S
        S = C @ F + D + s[:, None, None] * E

        return S

    def _get_S_from_model(self, s) -> np.ndarray:
        # Returns S-Parameters from the model without calculating the state space model
        # Input argument s is the complex frequency s = 1j * omega

        # Get n_ports
        n_ports = self._get_n_ports()

        # Get n_freqs
        n_freqs = np.size(s, axis = 0)

        # Initialize output matrix
        S = np.empty((n_freqs, n_ports, n_ports), dtype = complex)

        # Build S
        for i in range(n_ports):
            for j in range(n_ports):
                idx_response = i * n_ports + j

                # Get pole group index
                idx_pole_group=self.map_idx_response_to_idx_pole_group[idx_response]

                # Get pole group member index
                idx_pole_group_member=self.map_idx_response_to_idx_pole_group_member[idx_response]

                # Get data
                poles = self.poles[idx_pole_group]
                residues = self.residues[idx_pole_group][idx_pole_group_member]
                constant = self.constant[idx_pole_group][idx_pole_group_member]
                proportional = self.proportional[idx_pole_group][idx_pole_group_member]

                # Calculate S
                S[:, i, j] = proportional * s + constant

                for idx_pole, pole in enumerate(poles):
                    if np.imag(pole) == 0.0:
                        # Real pole
                        S[:, i, j] += residues[idx_pole] / (s - pole)
                    else:
                        # Complex conjugate pole
                        S[:, i, j] += \
                            residues[idx_pole] / (s - pole) + \
                            np.conjugate(residues[idx_pole]) / (s - np.conjugate(pole))

        return S

    def passivity_test(self,
        parameter_type: str = 's',
        verbose: bool = False,
        method = None,
        reltol_hamiltonian = 1e-3,
        n_samples_sampling = 10000,
        range_sampling = None,
        ):
        """
        Evaluates the passivity of reciprocal vector fitted models by means of a half-size test matrix [#]_. Any
        existing frequency bands of passivity violations will be returned as a sorted list.

        Parameters
        ----------
        parameter_type: str, optional
            Representation type of the fitted frequency responses. Either *scattering* (:attr:`s` or :attr:`S`),
            *impedance* (:attr:`z` or :attr:`Z`) or *admittance* (:attr:`y` or :attr:`Y`). Currently, only scattering
            parameters are supported for passivity evaluation.

        method: str, optional
            Can be set to 'half-size' or 'hamiltonian' for force one or the other method. Note that if the matrix
            is not symmetric and half-size is specified, the hamiltonian test will be used instead.

        Raises
        ------
        NotImplementedError
            If the function is called for `parameter_type` different than `S` (scattering).

        ValueError
            If the function is used with a model containing nonzero proportional coefficients.

        Returns
        -------
        violation_bands : ndarray
            NumPy array with frequency bands of passivity violation:
            `[[f_start_1, f_stop_1], [f_start_2, f_stop_2], ...]`.

        See Also
        --------
        is_passive : Query the model passivity as a boolean value.
        passivity_enforce : Enforces the passivity of the vector fitted model, if required.

        Examples
        --------
        Load and fit the `Network`, then evaluate the model passivity:

        >>> nw_3port = skrf.Network('my3port.s3p')
        >>> vf = skrf.VectorFitting(nw_3port)
        >>> vf.vector_fit(n_poles_real=1, n_poles_cmplx=4)
        >>> violations = vf.passivity_test()

        References
        ----------
        .. [#] B. Gustavsen and A. Semlyen, "Fast Passivity Assessment for S-Parameter Rational Models Via a Half-Size
            Test Matrix," in IEEE Transactions on Microwave Theory and Techniques, vol. 56, no. 12, pp. 2701-2708,
            Dec. 2008, DOI: 10.1109/TMTT.2008.2007319.
        """

        if parameter_type.lower() != 's':
            raise NotImplementedError('Passivity testing is currently only supported for scattering (S) parameters.')
        if parameter_type.lower() == 's' and not self._all_proportional_are_zero():
            raise ValueError('Passivity testing of scattering parameters with nonzero proportional coefficients does '
                             'not make any sense; you need to run vector_fit() with option `fit_proportional=False` '
                             'first.')

        # Return only the violation bands for the specified pole group
        violation_bands = self._passivity_test(
            verbose, method, reltol_hamiltonian, n_samples_sampling, range_sampling)

        return violation_bands

    def _passivity_test(self,
        verbose = False,
        method = None,
        reltol_hamiltonian = 1e-3,
        n_samples_sampling = 20000,
        range_sampling = None,
        ) -> np.ndarray:

        # Runs either the half size or hamiltonian passivity test, depending on symmetry.
        # Description of arguments see passivity_test
        #
        # Note on np.allclose: If the following equation is element-wise True, then allclose returns True.:
        # absolute(a - b) <= (atol + rtol * absolute(b))
        #
        # The default value of atol is not appropriate when the reference value b has magnitude smaller than one.
        # For example, it is unlikely that a = 1e-9 and b = 2e-9 should be considered “close”, yet allclose(1e-9, 2e-9)
        # is True with default settings. Be sure to select atol for the use case at hand, especially for defining the
        # threshold below which a non-zero value in a will be considered “close” to a very small or zero value in b.
        # Defaults: rtol=1e-05, atol=1e-08
        #
        # Note: I leave it at default atol for now but this should be investigated and maybe adjusted.

        # Check symmetry
        is_symmetric = self.is_symmetric()

        # Run sampling test if requested
        if method is not None:
            method = method.lower()
            if method == 'sampling':
                if verbose:
                    print("Using sampling based passivity test on request")
                return self._passivity_test_sampling(n_samples_sampling, range_sampling)

            elif method == 'hamiltonian':
                if verbose:
                    print("Using full size hamiltonian passivity test on request")
                return self._passivity_test_hamiltonian(reltol_hamiltonian)

            elif method == 'half-size':
                if verbose:
                    print("Using half size passivity test on request")

                # Warn if not symmetric
                if not is_symmetric:
                    warnings.warn('Using half size passivity test but matrix is not symmetric. Expect '
                                  'wrong results.', RuntimeWarning, stacklevel=2)
                return self._passivity_test_half_size()
            else:
                warnings.warn('Unknown passivity test method specified ', RuntimeWarning, stacklevel=2)

        # Not method specified

        # Run passivity test depending on symmetry
        if not is_symmetric:
            # If not symmetric we always use the hamiltonian test
            if verbose:
                print("Matrix is not symmetric. Using full size hamiltonian passivity test.")
            return self._passivity_test_hamiltonian(reltol_hamiltonian)
        else:
            # If symmetric, we use half-size by default
            if verbose:
                print("Matrix is symmetric. Using fast half size passivity test.")
            return self._passivity_test_half_size()

    def _passivity_test_hamiltonian(self,  reltol = 1e-9) -> np.ndarray:
        # Hamiltonian based passivity test. Description of arguments see passivity_test
        # Works also for non-symmetric state space model
        #
        # The operator @ is the same as numpy.matmul()

        # Get state-space matrices
        A, B, C, D, E = self._get_state_space_ABCDE()

        n_ports = np.shape(D)[0]

        # Build hamiltonian matrix M.
        # As defined in equation 8 in "Fast Passivity Assessment for S -Parameter Rational Models Via
        # a Half-Size Test Matrix", Bjørn Gustavsen and Adam Semlyen, 2008
        R_roof_inv = np.linalg.inv(np.transpose(D) @ D - np.identity(n_ports))
        S_roof_inv = np.linalg.inv(D @ np.transpose(D) - np.identity(n_ports))
        M11 = A - B @ R_roof_inv @ np.transpose(D) @ C
        M12 = -1 * B @ R_roof_inv @ np.transpose(B)
        M21 = np.transpose(C) @ S_roof_inv @ C
        M22 = -1 * np.transpose(A) + np.transpose(C) @ D @ R_roof_inv @ np.transpose(B)
        M = np.block([[M11, M12], [M21, M22]])

        # Calculate eigenvalues of M
        eigvals_M = np.linalg.eigvals(M)

        # The eigvals of M will be either real or complex conjugated pairs.
        # Additionally, we are only interested in purely imaginary eigenvalues because those are the
        # crossover frequencies.
        # Due to noise we can still have a very small non-zero real part so we compare them against the absolute value
        # and set a threshold.

        # Remove purely real eigenvalues
        eigvals_M = eigvals_M[(np.imag(eigvals_M) != 0)]

        # Remove eigenvalues that are not purely imaginary
        eigvals_M = eigvals_M[(np.abs(np.real(eigvals_M)) < reltol * np.abs(np.imag(eigvals_M)))]

        # Take only the imaginary parts
        eigvals_M = np.imag(eigvals_M)

        # Remove negative-imaginary eigenvalues of complex conjugate pairs and we obtain the crossover frequencies
        # at which the singular values cross unity
        crossover_omegas = eigvals_M[(eigvals_M > 0)]

        # Now we know only the crossover frequencies at which the singular values cross unity but we don't know yet
        # whether we went above or below unity. Identify frequency bands of passivity violations
        violation_bands = self._get_violation_bands(A, B, C, D, E, crossover_omegas)

        return violation_bands

    def _passivity_test_half_size(self) -> np.ndarray:
        # Half-size-matrix passivity test. Description of arguments see passivity_test
        #
        # The responses that are used to create the state space model must be symmetric because the algorithm assumes
        # that the state space model is also symmetric. This means that the residues, proportional and constant all
        # must be symmetric. This needs to be ensured before calling this method.
        #
        # The operator @ is the same as numpy.matmul()

        # Get state-space matrices
        A, B, C, D, E = self._get_state_space_ABCDE()

        n_ports = np.shape(D)[0]

        # Build half-size test matrix P from state-space matrices A, B, C, D
        inv_neg = np.linalg.inv(D - np.identity(n_ports))
        inv_pos = np.linalg.inv(D + np.identity(n_ports))
        P = (A - B @ inv_neg @ C) @ (A - B @ inv_pos @ C)

        # Extract eigenvalues of P
        P_eigs = np.linalg.eigvals(P)

        # Purely imaginary square roots of eigenvalues identify frequencies (2*pi*f) of borders of passivity violations
        P_eigs_sqrt = np.sqrt(P_eigs)

        # Keep only those eigvals of P with a zero real part
        P_eigs_sqrt = P_eigs_sqrt[np.real(P_eigs_sqrt) == 0]

        # Crossover frequencies are the purely imaginary elements
        crossover_omegas = np.imag(P_eigs_sqrt)

        # Now we know only the crossover frequencies at which the singular values cross unity but we don't know yet
        # whether we went above or below unity. Identify frequency bands of passivity violations
        violation_bands = self._get_violation_bands(A, B, C, D, E, crossover_omegas)

        return violation_bands

    def _passivity_test_sampling(self, n_samples, range_sampling) -> np.ndarray:
        # Sampling based passivity test. Least reliable because violations can easily be missed if they occur
        # between two consecutive samples.

        # Get min and max omega
        if range_sampling is not None:
            omega_min = range_sampling[0]
            omega_max = range_sampling[1]
        else:
            omega_min = 0
            omega_max = 2 * np.pi * self.network.f[-1] * 10

        # Create frequencies for sampling
        omega_eval = np.linspace(omega_min, omega_max, n_samples)
        s_eval = 1j * omega_eval

        print(f'Sampling based passivity test: range=[{omega_min:.1e}, {omega_max:.1e}] delta={omega_eval[1] - omega_eval[0]:.1e}')

        # Calculate singular values for all sampling frequencies
        u, sigma, vh = np.linalg.svd(self._get_S_from_model(s_eval))

        # Get maximum over all sigmas
        sigma = np.max(sigma, axis = 1)

        # Initialize violation bands list
        violation_bands = []

        # Convert sigma to list
        sigma = sigma.tolist()

        # Flag that is true if we are inside of a voilation band
        is_inside_band = False

        # Check if first sigma is above 1
        if sigma[0] > 1:
            current_band = [0, 0]
            is_inside_band = True

        for i, sigma in enumerate(sigma):
            if is_inside_band and sigma < 1:
                current_band[1] = omega_eval[i - 1]
                violation_bands.append(current_band)
                is_inside_band = False
                continue

            if sigma > 1 and not is_inside_band:
                # Start a new band
                current_band = [omega_eval[i], omega_eval[i]]
                is_inside_band = True

        # Check last band
        if is_inside_band:
            current_band[1] = float('Inf')
            violation_bands.append(current_band)
            is_inside_band = False

        return np.array(violation_bands)

    def _get_violation_bands(self, A, B, C, D, E, crossover_omegas) -> np.ndarray:
        # Calculates the violation bands at which the singular values are above unity.
        # The input is the state space model and a list of crossover frequencies
        # at which the singular values cross unity

        # Include dc (0) unless it's already included. We need this for the next step because we will probe every
        # interval whether it is above or below unity. If we have a first crossing at x, the first band that we will
        # probe is [0, x].
        if len(np.nonzero(crossover_omegas == 0.0)[0]) == 0:
            crossover_omegas = np.append(crossover_omegas, 0)

        # Sort the output from lower to higher frequencies
        crossover_omegas = np.sort(crossover_omegas)

        # Identify bands of passivity violations
        violation_bands = []
        for i, omega in enumerate(crossover_omegas):
            if i == len(crossover_omegas) - 1:
                # Last band stops always at infinity
                omega_start = omega
                omega_stop = np.inf
                s_probe = 1j * 1.1 * omega_start # 1.1 is chosen arbitrarily to have any frequency for evaluation
            else:
                # Intermediate band between this frequency and the previous one
                omega_start = omega
                omega_stop = crossover_omegas[i + 1]
                s_probe = 1j * 0.5 * (omega_start + omega_stop)

            # Calculate singular values at the center frequency between crossover frequencies to identify violations
            # Todo: What is faster, via state space or via model directly?
            S_probe = self._get_S_from_state_space_ABCDE(np.array([s_probe]), A, B, C, D, E)
            # S_probe2 = self._get_S_from_model(np.array([s_probe]))

            sigma = np.linalg.svd(S_probe[0], compute_uv=False)

            # Check if all singular values are less than unity
            is_passive = len(np.nonzero(sigma[sigma > 1])[0]) == 0

            if not is_passive:
                # Add this band to the list of passivity violations
                violation_bands.append([omega_start, omega_stop])

        return np.array(violation_bands)

    def is_symmetric(self) -> bool:
        # Checks whether the model is symmetric

        # Symmetry can only be achieved if all poles are in one group.
        if len(self.poles) != 1:
            return False

        # Get number of responses
        n_responses = self._get_n_responses(0)

        # Symmetry can only be achieved if we have at least 4 responses
        if n_responses < 4:
            return False

        # Symmetry can only be achieved if the square root of n_responses has no fractional part
        if np.mod(np.sqrt(n_responses), 1) != 0:
            return False

        # Get residues and constant of the first and only pole group 0
        residues = self.residues[0]
        # Calculate n_matrix
        n_matrix = int(np.sqrt(n_responses))
        # Reshape only the first residue of all responses into
        # size sqrt(n_responses) x sqrt(n_responses). It is assumed that for the other residues
        # the symmetry will be the same.
        residues = np.reshape(residues[:, 0], shape=(n_matrix, n_matrix))
        # Test residues for symmetry
        if not issymmetric(residues, rtol=1e-5):
            return False

        # Get constant
        constant = self.constant[0]
        # Reshape constant into sqrt(n_responses) x sqrt(n_responses)
        constant = np.reshape(constant, shape=(n_matrix, n_matrix))
        # Test constant for symmetry
        if not issymmetric(constant, rtol=1e-5):
            return False

        # Otherwise we are symmetric! Yay!
        return True

    def is_passive(self, idx_pole_group = None, parameter_type: str = 's') -> bool:
        """
        Returns the passivity status of the model as a boolean value.

        Parameters
        ----------
        parameter_type : str, optional
            Representation type of the fitted frequency responses. Either *scattering* (:attr:`s` or :attr:`S`),
            *impedance* (:attr:`z` or :attr:`Z`) or *admittance* (:attr:`y` or :attr:`Y`). Currently, only scattering
            parameters are supported for passivity evaluation.

        Returns
        -------
        passivity : bool
            :attr:`True` if model is passive, else :attr:`False`.

        See Also
        --------
        passivity_test : Verbose passivity evaluation routine.
        passivity_enforce : Enforces the passivity of the vector fitted model, if required.

        Examples
        --------
        Load and fit the `Network`, then check whether or not the model is passive:

        >>> nw_3port = skrf.Network('my3port.s3p')
        >>> vf = skrf.VectorFitting(nw_3port)
        >>> vf.vector_fit(n_poles_real=1, n_poles_cmplx=4)
        >>> vf.is_passive() # returns True or False
        """

        violation_bands = self.passivity_test(parameter_type)

        # Return false if violation bands is not empty
        return len(violation_bands) == 0

    def passivity_enforce(self,
        n_samples: int = 200,
        n_samples_per_band: int = 200,
        maximum_frequency_of_interest: float = None,
        parameter_type: str = 's',
        max_iterations: int = 100,
        verbose = False,
        perturb_constant = False,
        asymptotic_method = 'optimizer',
        preserve_dc = True,
        ) -> None:
        """
        Enforces the passivity of the vector fitted model, if required. This is an implementation of the method
        presented in [#]_. Passivity is achieved by updating the residues and the constants.

        Parameters
        ----------
        n_samples: int, optional
            Number of linearly spaced frequency samples at which passivity will be evaluated and enforced.
            (Default: 100)

        maximum_frequency_of_interest: float or None, optional
            Highest frequency of interest for the passivity enforcement (in Hz, not rad/s). This limit usually
            equals the highest sample frequency of the fitted Network. If None, the highest frequency in
            :attr:`self.network` is used, which must not be None is this case. If `f_max` is not None, it overrides the
            highest frequency in :attr:`self.network`.

        parameter_type: str, optional
            Representation type of the fitted frequency responses. Either *scattering* (:attr:`s` or :attr:`S`),
            *impedance* (:attr:`z` or :attr:`Z`) or *admittance* (:attr:`y` or :attr:`Y`). Currently, only scattering
            parameters are supported for passivity evaluation.

        perturb_constant: bool, optional
            Enables or disables constant perturbation in passivity enforcement. If this is enabled, the DC point
            may be affected in a negative way. It is thus disabled by default. Use it only if passivity enforcement
            is not successful without it.

        preserve_dc:
            Enables the DC preserving passivity enforcement. Must be set to the same value that was used for
            preserve_dc in auto_fit or vector_fit.

        Returns
        -------
        None

        Raises
        ------
        NotImplementedError
            If the function is called for `parameter_type` different than `S` (scattering).

        ValueError
            If the function is used with a model containing nonzero proportional coefficients. Or if both `f_max` and
            :attr:`self.network` are None.

        See Also
        --------
        is_passive : Returns the passivity status of the model as a boolean value.
        passivity_test : Verbose passivity evaluation routine.
        plot_passivation : Convergence plot for passivity enforcement iterations.

        Examples
        --------
        Load and fit the `Network`, then enforce the passivity of the model:

        >>> nw_3port = skrf.Network('my3port.s3p')
        >>> vf = skrf.VectorFitting(nw_3port)
        >>> vf.vector_fit(n_poles_real=1, n_poles_cmplx=4)
        >>> vf.passivity_enforce()  # won't do anything if model is already passive

        References
        ----------
        .. [#] T. Dhaene, D. Deschrijver and N. Stevens, "Efficient Algorithm for Passivity Enforcement of S-Parameter-
            Based Macromodels," in IEEE Transactions on Microwave Theory and Techniques, vol. 57, no. 2, pp. 415-420,
            Feb. 2009, DOI: 10.1109/TMTT.2008.2011201.
        """

        if preserve_dc:
            self._passivity_enforce_preserve_dc(
                n_samples, n_samples_per_band, maximum_frequency_of_interest, parameter_type,
                max_iterations, verbose, asymptotic_method)
        else:
            self._passivity_enforce(
                n_samples, n_samples_per_band, maximum_frequency_of_interest, parameter_type,
                max_iterations, verbose, perturb_constant)

        # Print model summary
        self.print_model_summary(verbose)

        # Run final passivity test to make sure passivation was successful
        violation_bands = self.passivity_test(parameter_type=parameter_type)

        if not self.is_passive(parameter_type=parameter_type):
            warnings.warn('Passivity enforcement was not successful.\nModel is still non-passive in these frequency '
                          f'bands:\n{violation_bands}.\nTry running this routine again with a larger number of samples '
                          '(parameter `n_samples`).', RuntimeWarning, stacklevel=2)

    def _passivity_enforce(self,
        n_samples,
        n_samples_per_band,
        maximum_frequency_of_interest,
        parameter_type,
        max_iterations,
        verbose,
        perturb_constant,
        ) -> None:
        # Passivity enforcement. Description of arguments see passivity_enforce()
        #
        # The method is presented in:
        # "Efficient Algorithm for Passivity Enforcement of S -Parameter-Based Macromodels",
        # Tom Dhaene, Dirk Deschrijver, Nobby Stevens,
        # IEEE Transactions On Microwave Theory And Techniques, Vol. 57, No. 2, February 2009

        if parameter_type.lower() != 's':
            raise NotImplementedError('Passivity testing is currently only supported for scattering (S) parameters.')
        if parameter_type.lower() == 's' and len(np.flatnonzero(self.proportional)) > 0:
            raise ValueError('Passivity testing of scattering parameters with nonzero proportional coefficients does '
                             'not make any sense; you need to run vector_fit() with option `fit_proportional=False` '
                             'first.')

        # Run passivity test first
        if self.is_passive(parameter_type):
            # Model is already passive; do nothing and return
            logger.info('Passivity enforcement: The model is already passive. Nothing to do.')
            return

        # First, dense set of frequencies is determined from dc up to about 20% above the highest relevant frequency.
        # This highest relevant frequency is the maximum of the highest crossing from a nonpassive to a passive region
        # on one hand and the maximum frequency of interest on the other hand [1]

        # Get violation bands
        violation_bands = self.passivity_test(parameter_type)

        # Get highest crossing from a nonpassive to a passive region
        omega_highest_crossing = violation_bands[-1, 1]

        # Deal with unbounded violation interval (omega_highest_crossing == np.inf)
        if np.isinf(omega_highest_crossing):
            # The paper doesn't specify what to do in this case. It i set to 1.5 omega_start for now
            # but I don't understand the implications of this yet. It's certainly not a crossing from a nonpassive
            # to a passive region as specified in the paper.
            omega_highest_crossing = 1.5 * violation_bands[-1, 0]
            warnings.warn(
                'Passivity enforcement: The passivity violations of this model are unbounded. '
                'Passivity enforcement might still work, but consider re-fitting with a lower number of poles '
                'and/or without the constants (`fit_constant=False`) if the results are not satisfactory.',
                UserWarning, stacklevel=2)

        # Check if maximum_frequency_of_interest is specified
        if maximum_frequency_of_interest is None:
            # Check if we have a netwoork
            if self.network is None:
                raise RuntimeError('Both `self.network` and parameter `maximum_frequency_of_interest` are None. One of them is required to '
                                   'specify the frequency band of interest for the passivity enforcement.')
            else:
                # Set maximum_frequency_of_interest to highest frequency of network
                maximum_frequency_of_interest = self.network.f[-1]

        # Calculate omega
        maximum_omega_of_interest = 2 * np.pi * maximum_frequency_of_interest

        # Get s_eval
        omega_eval, s_eval = self._passivity_get_eval_frequencies(
            maximum_omega_of_interest, n_samples, n_samples_per_band, parameter_type)

        # Set tolerance parameter according to paper. Unfortunately it does not provide any information on
        # how this parameter influences the algorithm.
        # This parameter has a strong influence on the rms error in some tests I ran. Using 1-1e-4 instead
        # of 1-1e-3 resulted in about 100x less rms error. On the other hand, it does not converge for 1-1e-5.
        # So this algorithm seems to be extremely sensitive to the value of delta which is contradicting the
        # paper that's not specific about the value of delta.
        delta = 1-1e-3

        # Get state space model
        F, A, B, C, D, E, F_view, A_view, B_view, C_view, nC, C_col_idx_begin, C_col_idx_end = \
            self._get_state_space_FABCDE(s_eval, create_modified = False, create_views = True)

        # Flag that's True if we have non zero D
        have_D = len(np.nonzero(D)[0]) != 0

        # Get the number of ports
        n_ports = self._get_n_ports()

        # Initialize A_ls as a two dimensional list with None for the least squares
        A_ls = [x[:] for x in [[None] * n_ports] * n_ports]

        # Initialize D_norm
        D_norm = [x[:] for x in [[None] * n_ports] * n_ports]

        if verbose:
            if have_D and perturb_constant:
                print('Perturbing residues and constant')
            else:
               print('Perturbing only residues')

        # Build F0_transpose
        for i in range(n_ports):
            for j in range(n_ports):
                # Get F0
                F0 = F_view[i][j]

                # Build matrix F1 that contains F0 and optionally a row for D if we have it
                if have_D:
                    F1 = np.empty((np.size(F0, axis = 0), np.size(F0, axis = 1) + 1), dtype=complex)
                    D_norm[i][j] = np.linalg.norm(F0) / np.size(F0)
                    F1[:, :-1] = F0
                    F1[:, -1] = 1 * D_norm[i][j][None, None]
                else:
                    F1 = F0

                # Transpose F. We can transpose and squeeze the size 1 dimension in 1 go:
                F1_transpose = np.squeeze(F1)

                # Build A_ls for the least squares problem A x = b
                A_ls[i][j] = np.vstack((np.real(F1_transpose), np.imag(F1_transpose)))

        # Save C to compare after perturbation
        C_original = np.copy(C)

        # Iterative compensation of passivity violations
        iteration = 0
        while iteration < max_iterations:
            logger.info(f'Passivity enforcement: Iteration {iteration + 1}')

            # Get S
            S = C @ F + D

            # Singular value decomposition
            u, sigma, vh = np.linalg.svd(S, full_matrices=False)

            # Debug: Plot the frequency response of each singular value
            # import matplotlib.pyplot as plt
            # fig, ax = plt.subplots()
            # ax.grid()
            # for n in range(np.size(sigma, axis=1)):
            #     ax.plot(omega_eval, sigma[:, n], label=fr'$\sigma$ idx_pole_group={idx_pole_group + 1}, index={n + 1}')
            # ax.set_xlabel('Frequency (rad)')
            # ax.set_ylabel('Magnitude')
            # ax.legend(loc='best')
            # plt.show()

            # Maximum singular value
            sigma_max = np.max(sigma)

            # Stop iterations if model is passive
            if sigma_max <= 1.0:
                break

            # Set all sigma that are <= delta to zero
            sigma[sigma <= delta] = 0

            # Subtract delta from all sigma that are > delta
            sigma[sigma > delta] -= delta

            # Calculate S_viol
            S_viol = (u * sigma[:, None, :]) @ vh

            # Solve C_viol for every response
            for i in range(n_ports):
                for j in range(n_ports):
                    # Solve overdetermined least squares problem for Cviol

                    # Solve S_viol = C_viol F for C_viol. This is a system of the form x A = b but
                    # because (AB)^T = B^T A^T, we can convert it into a system of form A x = b by transposing:
                    #
                    # Solve F^T C_viol^T = S_viol^T for C_viol^T
                    # C_viol is of shape 1 x n_poles and
                    # F is of shape n_poles x n_poles and
                    # S_viol is of shape 1 x 1, so S_viol^T == S_viol
                    # (of course in addition to that we have the outermost dimension for the frequency for all of them)

                    # Build b_ls for the least squares problem A x = b
                    b_ls = np.hstack((np.real(S_viol[:, i, j]), np.imag(S_viol[:, i, j])))

                    # Solve least squares
                    x, residuals, rank, singular_values = np.linalg.lstsq(A_ls[i][j], b_ls, rcond=None)

                    # Perturb C and D
                    if have_D:
                        C_view[i][j][:] -= x[:-1]
                        if perturb_constant:
                            D[i, j] -= x[-1] * D_norm[i][j]
                    else:
                        C_view[i][j][:] -= x

            # Increment iteration counter
            iteration += 1

        # Calculate dC/C
        C_delta = C - C_original
        C_delta_norm_rel = \
            np.linalg.norm(C_delta, ord='fro') / np.linalg.norm(C_original, ord='fro')
        print(f'Passivity enforcement dC/C = {C_delta_norm_rel:.1e}')

        # Warn if maximum number of iterations has been exceeded
        if iteration == max_iterations:
            warnings.warn('Passivity enforcement: Aborting after the max. number of iterations has been '
                          'exceeded.', RuntimeWarning, stacklevel=2)
            return

        # Update model
        self._passivity_update_model(C_view,
            preserve_dc = False, have_D = have_D, perturb_constant = perturb_constant, D = D)

        print(f'Finished passivity enforcement after {iteration} iterations')

    def _passivity_enforce_preserve_dc(self,
        n_samples,
        n_samples_per_band,
        maximum_frequency_of_interest,
        parameter_type,
        max_iterations,
        verbose,
        asymptotic_method = 'least-squares',
        ) -> None:
        # DC preserving passivity enforcement. Description of arguments see passivity_enforce()
        #
        # The method is presented in:
        # "DC-Preserving Passivity Enforcement for S-Parameter Based Macromodels", Dirk Deschrijver, Tom Dhaene
        # IEEE Transactions On Microwave Theory And Thechniques, Vol. 58, No. 4, April 2010

        if parameter_type.lower() != 's':
            raise NotImplementedError('Passivity testing is currently only supported for scattering (S) parameters.')

        if parameter_type.lower() == 's' and len(np.flatnonzero(self.proportional)) > 0:
            raise ValueError('Passivity testing of scattering parameters with nonzero proportional coefficients does '
                             'not make any sense; you need to run vector_fit() with option `fit_proportional=False` '
                             'first.')

        # Return if passive
        if self.is_passive(parameter_type):
            logger.info('Model is passive. Skipping passivity enforcement')
            return

        # Check if maximum_frequency_of_interest is specified
        if maximum_frequency_of_interest is None:
            # Check if we have a netwoork
            if self.network is None:
                raise RuntimeError('Both `self.network` and parameter `maximum_frequency_of_interest` are None. '
                                   'One of them is required to specify the frequency band of interest for the '
                                   'passivity enforcement.')
            else:
                # Set maximum_frequency_of_interest to highest frequency of network
                maximum_frequency_of_interest = self.network.f[-1]

        # Calculate omega
        maximum_omega_of_interest = 2 * np.pi * maximum_frequency_of_interest

        # Get n_ports
        n_ports = self._get_n_ports()

        # Asymptotic passivity enforcement.

        # Get s_eval
        omega_eval, s_eval = self._passivity_get_eval_frequencies(
            maximum_omega_of_interest, n_samples, n_samples_per_band, parameter_type)

        # Get state space model
        F_modified, A_modified, B_modified, C_modified, D_modified, E_modified, \
            F_modified_view, A_modified_view, B_modified_view, C_modified_view, \
            nC, C_col_idx_begin, C_col_idx_end = \
            self._get_state_space_FABCDE(s_eval, create_views = True, create_modified = True)

        F, A, B, C, D, E, \
            F_view, A_view, B_view, C_view, \
            nC, C_col_idx_begin, C_col_idx_end = \
            self._get_state_space_FABCDE(s_eval, create_views = True, create_modified = False)

        # Singular value decomposition
        u, sigma, vh = np.linalg.svd(D, full_matrices=False)

        # Maximum singular value
        sigma_max = np.max(sigma)

        # Debug: Plot the frequency response of each singular value
        # import matplotlib.pyplot as plt
        # fig, ax = plt.subplots()
        # ax.grid()
        # for n in range(np.size(sigma, axis=1)):
        #     ax.plot(omega_eval, sigma[:, n], label=fr'$\sigma$ idx_pole_group={idx_pole_group + 1}, index={n + 1}')
        # ax.set_xlabel('Frequency (rad)')
        # ax.set_ylabel('Magnitude')
        # ax.legend(loc='best')
        # plt.show()

        # Continue if model is non-passive
        if sigma_max > 1:
            print('Starting asymptotic passivity enforcement.')

            # Set delta
            delta = 1

            # Set all sigma that are <= delta to zero
            sigma[sigma <= delta] = 0

            # Subtract delta from all sigma that are > delta
            sigma[sigma > delta] -= delta

            # Calculate S_viol
            S_viol = (u * sigma) @ vh

            if asymptotic_method == 'least-squares':
                # Create copy of C_modified for comparison after passivity enforcement
                C_modified_original = np.copy(C_modified)

                for i in range(n_ports):
                    for j in range(n_ports):
                        # Prepare A_ls and b_ls
                        A_ls = np.atleast_2d(B_view[i][j].T)
                        b_ls = np.expand_dims(S_viol[i,j], 0)

                        # Solve underdetermined least squares
                        x, residuals, rank, singular_values = np.linalg.lstsq(A_ls, b_ls, rcond=None)

                        # Update
                        C_modified_view[i][j][:] -= x

                # Calculate dC/C
                C_modified_delta = C_modified - C_modified_original
                C_modified_delta_norm_rel = \
                    np.linalg.norm(C_modified_delta, ord='fro') / np.linalg.norm(C_modified_original, ord='fro')
                print(f'Asymptotic passivity enforcement dC/C = {C_modified_delta_norm_rel:.1e}')
                print(f'delta: {C_modified_delta}')

            elif asymptotic_method == 'optimizer':
                # Original response
                #S_original = C_modified @ F_modified + D_modified # See cost_function

                # Create working copy of C_modified for optimization
                _C_modified = np.copy(C_modified)

                def flat_C_to_matrix_C(C_flat, C_matrix):
                    # Reshape the flat C vector into matrix form
                    offs = 0
                    for i in range(n_ports):
                        for j in range(n_ports):
                            j_beg = C_col_idx_begin[i][j]
                            j_end = C_col_idx_end[i][j]
                            n = j_end - j_beg
                            C_matrix[i, j_beg:j_end] = C_flat[offs:offs + n]
                            offs += n

                def matrix_C_to_flat_C(C_flat, C_matrix):
                    # Reshape the flat C vector into matrix form
                    offs = 0
                    for i in range(n_ports):
                        for j in range(n_ports):
                            j_beg = C_col_idx_begin[i][j]
                            j_end = C_col_idx_end[i][j]
                            n = j_end - j_beg
                            C_flat[offs : offs + n] = C_matrix[i, j_beg:j_end]
                            offs += n

                # Weighting factors for cost function
                alpha = 100.0 # Weight for equation fidelity
                beta = 1.0   # Weight for preserving original model
                # gamma = 0.01 # Regularization weight for smoothness. See cost_function

                # Define cost function
                def cost_function(C_modified_flat):
                    # Reshape the flat C vector into matrix form
                    flat_C_to_matrix_C(C_modified_flat, _C_modified)

                    # Compute the reconstructed S_viol from C and B
                    S_viol_reconstructed = _C_modified @ B

                    # Fidelity to the equation S_viol = C * B
                    fidelity_term = alpha * np.linalg.norm(S_viol - S_viol_reconstructed, ord='fro')**2

                    # Deviation from the original S matrix (all frequencies)
                    # Disabled because it is very costly
                    #
                    #S_modified = (C_modified - _C_modified) @ F_modified + D_modified  # Assuming passivity adjustments
                    #deviation_term = 0
                    #for i in range(0, np.size(S_original, axis = 0), 20 ):
                    #    deviation_term += np.linalg.norm(S_original[i] - S_modified[i], ord='fro')**2
                    #deviation_term = beta * deviation_term# / np.size(S_original, axis = 0)
                    #
                    # Minimizing norm of dC instead
                    # Calculate dC
                    #deviation_term = beta * np.linalg.norm(_C_modified - C_modified, ord = 'fro')**2
                    deviation_term = beta * np.linalg.norm(_C_modified, ord = 'fro')**2

                    #print(f'f={fidelity_term} d={deviation_term}')
                    # Regularization for smoothness
                    #regularization_term = gamma * np.linalg.norm(_C_modified, ord='fro')**2

                    return fidelity_term + deviation_term# + regularization_term

                # Flatten the initial guess for C
                #C_modified_initial_flat = np.zeros(int(np.sum(nC)))
                C_modified_initial_flat = np.dot(S_viol, np.linalg.pinv(B)).flatten()
                #matrix_C_to_flat_C(C_modified_initial_flat, C_modified)

                # Optimization
                result = minimize(cost_function, C_modified_initial_flat, method='L-BFGS-B')

                # Extract optimized C_viol
                flat_C_to_matrix_C(result.x, _C_modified)

                # Calculate dC/C
                C_modified_delta_norm_rel = \
                    np.linalg.norm(_C_modified, ord='fro') / np.linalg.norm(C_modified, ord='fro')
                print(f'Asymptotic passivity enforcement dC/C = {C_modified_delta_norm_rel:.1e}')
                print(f'delta: {-1 * _C_modified}')
                # Calculate C_asymp and subtract from C_modified
                C_modified -= _C_modified

            print(f'C_modified={C_modified}')
            # Update model
            self._passivity_update_model(C_modified_view, preserve_dc = True)

            print('Finished asymptotic passivity enforcement.')

            # Return if passive
            if self.is_passive(parameter_type):
                # Model is passive
                print('Model is passive. Skipping uniform passivity enforcement.')
                return

            else:
                # Update state space model for uniform passivity enforcement

                # Get s_eval
                omega_eval, s_eval = self._passivity_get_eval_frequencies(
                    maximum_omega_of_interest, n_samples, n_samples_per_band, parameter_type)

                # Get state space model
                F_modified, A_modified, B_modified, C_modified, D_modified, E_modified, \
                    F_modified_view, A_modified_view, B_modified_view, C_modified_view, \
                    nC, C_col_idx_begin, C_col_idx_end = \
                    self._get_state_space_FABCDE(s_eval, create_views = True, create_modified = True)

                F, A, B, C, D, E, \
                    F_view, A_view, B_view, C_view, \
                    nC, C_col_idx_begin, C_col_idx_end = \
                    self._get_state_space_FABCDE(s_eval, create_views = True, create_modified = False)

                logger.info("Updated model")

        else:
            print('Model is asymptotically passive. Skipping asymptotic passivity enforcement.')

        # Uniform passivity enforcement
        print('Starting uniform passivity enforcement')

        # Initialize A_ls as a two dimensional list with None for the least squares
        A_ls = [x[:] for x in [[None] * n_ports] * n_ports]
        # weights_ls = [x[:] for x in [[None] * n_ports] * n_ports] # TODO: See comments below on weighting

        # Build F0_modified_transpose
        for i in range(n_ports):
            for j in range(n_ports):
                # Get F0
                F0_modified = F_modified_view[i][j]

                # Transpose F. We can transpose and squeeze the size 1 dimension in 1 go:
                #F0_modified_transpose = np.squeeze(F0_modified)[1:] # TODO: Unclear: LS without DC point?
                F0_modified_transpose = np.squeeze(F0_modified)[0:] # or with DC point?

                # Build A_ls for the least squares problem A x = b
                A_ls[i][j] = np.vstack((np.real(F0_modified_transpose), np.imag(F0_modified_transpose)))

                # TODO: LS with weighted equation rows or not?
                # If enabled, enable b weighting in the LS loop below using the same weights!
                #weights_ls[i][j] = np.linalg.norm(A_ls[i][j], axis = 1)
                #A_ls[i][j][:, :] = A_ls[i][j][:, :] / weights_ls[i][j][:, None]

        # Save C_modified to compare after perturbation
        C_modified_original = np.copy(C_modified)

        # Iterative compensation of passivity violations
        iteration = 0
        while iteration < max_iterations:
            # Get S
            S = C_modified @ F_modified + D_modified

            # Singular value decomposition
            u, sigma, vh = np.linalg.svd(S, full_matrices=False)

            # Debug: Plot the frequency response of each singular value
            # import matplotlib.pyplot as plt
            # fig, ax = plt.subplots()
            # ax.grid()
            # for n in range(np.size(sigma, axis=1)):
            #     ax.plot(omega_eval[:10], sigma[:10, 1], label=fr'$\sigma$ idx={n + 1}', marker='x')
            # ax.set_xlabel('Frequency (rad)')
            # ax.set_ylabel('Magnitude')
            # ax.legend(loc='best')
            # plt.show()

            # Maximum singular value
            sigma_max = np.max(sigma)
            logger.info(f'Uniform passivity enforcement: Iteration {iteration + 1} SigmaMax = {sigma_max}')

            # TODO: Improvement: Adaptive delta and adaptive sampling
            # 1. The closer delta is to 1, the better will be the fit after the passivation.
            #
            # 2. For delta = 1 - epsilon, the convergence will be faster for larger epsilon.
            #
            # 3. The sigma_max will decrease while this loop is running until it is below 1.
            #
            # 4. If we have a large epsilon when this crossing happens, the model will be changed more than necessary.
            #    resulting in a poorer fit after the passivation is done.
            #
            # 5. So what we actually want is a large epsilon in the beginning to converge fast and then, before we
            #    cross the 1 boundary, we want a very small epsilon, to not cross the boundary by more than necessary.
            #
            # 6. So basically we could make epsilon really small before crossing 1, like 1e-9 or something but it
            #    turns out that even if the sigma_max is less than one after this, the algebraic passivity tests
            #    in some cases still show that the model is non passive in a very narrow frequency band.
            #
            #    The reason for this unexpected result is that this passivation algorithm is based on a sampled
            #    evaluation of the sigmas with a "dense" set of frequencies. However, if the set is not dense enough,
            #    it can easily happen that all sampled sigmas are below 1 but still some sigmas between two samples
            #    can be above 1.
            #
            #    Now how is this related to epsilon? With a larger epsilon, we make a larger than necessary change
            #    to the model, crossing the boundary to 1 and going even a bit further. This 'going further below 1'
            #    helps to avoid the above described problem: Because we have now a sampled sigma that has quite a bit
            #    of margin to the 1 border, it is less likely that there are sigmas between two samples that go above 1.
            #
            #    My experiments showed that if I increasse the number of samples in the "dense set of frequencies",
            #    I can successfully go to 1e-4 or even lower with epsilon and still get a passive model. The only
            #    drawback is that the passivation process will be really slow if we increase the number of samples
            #    to a really high number.
            #
            # 7. Ideas to improve this: Adaptive sampling could be used that places much more samples inside of the
            #    violation intervals but less outside of them. This could enable us to use much smaller epsilons while
            #    still getting a passive model in the algegraic passivity tests.
            #
            epsilon = np.clip((sigma_max - 1) * 1.0, 1e-4, 1e-2)
            logger.info(f'delta=1-{epsilon:.3e}')
            delta = 1 - epsilon

            # Stop iterations if model is passive
            if sigma_max < 1.0:
                break

            # Set all sigma that are <= delta to zero
            sigma[sigma <= delta] = 0

            # Subtract delta from all sigma that are > delta
            sigma[sigma > delta] -= delta

            # Calculate S_viol
            #
            # TODO: Unclear: Should we subtract D from the S_viol? The least squares will not be able
            # to perturb D so the equations at DC with all zeros in A_ls will have a nonzero b that's
            # impossible to fit
            #S_viol = (((u * sigma[:, None, :]) @ vh) - D_modified)[1:] # Without D and without DC row
            #S_viol = (((u * sigma[:, None, :]) @ vh) - D_modified)[0:] # Without D and with DC row

            #S_viol = (((u * sigma[:, None, :]) @ vh))[1:] # TODO: Unclear: DC equation in LS system?
            S_viol = (((u * sigma[:, None, :]) @ vh))[0:] # or no DC equation in LS system? Match with A_ls above!

            # Solve C_viol for every response
            for i in range(n_ports):
                for j in range(n_ports):
                    # Solve overdetermined least squares problem for Cviol

                    # Solve S_viol = C_viol F for C_viol. This is a system of the form x A = b but
                    # because (AB)^T = B^T A^T, we can convert it into a system of form A x = b by transposing:
                    #
                    # Solve F^T C_viol^T = S_viol^T for C_viol^T
                    # C_viol is of shape 1 x n_poles and
                    # F is of shape n_poles x n_poles and
                    # S_viol is of shape 1 x 1, so S_viol^T == S_viol
                    # (of course in addition to that we have the outermost dimension for the frequency for all of them)

                    # Build b_ls for the least squares problem A x = b
                    b_ls = np.hstack((np.real(S_viol[:, i, j]), np.imag(S_viol[:, i, j])))

                    # TODO: Weighted LS equatins or not? Comments see above at A_ls!
                    #b_ls = np.hstack((np.real(S_viol[:, i, j]), np.imag(S_viol[:, i, j]))) / weights_ls[i][j]

                    # Solve least squares
                    x, residuals, rank, singular_values = np.linalg.lstsq(A_ls[i][j], b_ls, rcond=None)

                    # Perturb C
                    C_modified_view[i][j][:] -= x

            # Calculate dC/C
            C_modified_delta = C_modified - C_modified_original
            C_modified_delta_norm_rel = \
                np.linalg.norm(C_modified_delta, ord='fro') / np.linalg.norm(C_modified_original, ord='fro')
            logger.info(f'Uniform passivity enforcement dC/C = {C_modified_delta_norm_rel:.3e}')

            # Increment iteration counter
            iteration += 1

        # Calculate dC/C
        C_modified_delta = C_modified - C_modified_original
        C_modified_delta_norm_rel = \
            np.linalg.norm(C_modified_delta, ord='fro') / np.linalg.norm(C_modified_original, ord='fro')
        print(f'Uniform passivity enforcement dC/C = {C_modified_delta_norm_rel:.1e}')

        # Warn if maximum number of iterations has been exceeded
        if iteration == max_iterations:
            warnings.warn('Uniform passivity enforcement: Aborting after the max. number of iterations has been '
                          'exceeded.', RuntimeWarning, stacklevel=2)
            return

        # Update model
        self._passivity_update_model(C_modified_view, preserve_dc = True)

        print(f'Finished uniform passivity enforcement after {iteration} iterations')

    def _passivity_get_eval_frequencies(self,
        maximum_omega_of_interest, n_samples, n_samples_per_band, parameter_type):
        # Creates "dense set of frequencies" with n_samples from DC to highest_relevant_omega
        # and an additional n_samples_per_band for every violation band.

        # First, dense set of frequencies is determined from dc up to about 20% above the highest relevant frequency.
        # This highest relevant frequency is the maximum of the highest crossing from a nonpassive to a passive region
        # on one hand and the maximum frequency of interest on the other hand [1]

        # Get violation bands
        violation_bands = self.passivity_test(parameter_type)

        # Get highest crossing from a nonpassive to a passive region
        omega_highest_crossing = violation_bands[-1, 1]

        # Deal with unbounded violation interval (omega_highest_crossing == np.inf)
        if np.isinf(omega_highest_crossing):
            # The paper doesn't specify what to do in this case. I set it to 1.5 omega_start for now
            # but I don't understand the implications of this yet. It's certainly not a crossing from a nonpassive
            # to a passive region as specified in the paper.
            omega_highest_crossing = 1.5 * violation_bands[-1, 0]

            # Update last violation band
            violation_bands[-1, 1] =  omega_highest_crossing

            warnings.warn('Passivity violations are unbounded',
                UserWarning, stacklevel=2)

        # The frequency band for the passivity evaluation is from dc to 20% above the highest relevant frequency
        highest_relevant_omega = max(maximum_omega_of_interest, omega_highest_crossing)

        # Create omega_eval for every violation band
        n_bands = np.size(violation_bands, axis = 0)
        omega_eval_bands = np.empty((n_bands, n_samples_per_band))
        for i in range(n_bands):
            omega_eval_bands[i] = \
                np.linspace(violation_bands[i, 0], violation_bands[i, 1], n_samples_per_band)

        # Create omega_eval and s_eval
        omega_eval = np.append(
            np.linspace(0, 1.2 * highest_relevant_omega, n_samples),
            omega_eval_bands.flatten())

        s_eval = 1j * omega_eval

        return omega_eval, s_eval

    def _passivity_update_model(self, C_view,
        preserve_dc,
        have_D = False,
        perturb_constant = False,
        D = None,
        ):
        # Updates residues and constant of the model using state space C via C_view

        # Get the number of ports
        n_ports = self._get_n_ports()

        if preserve_dc:
            # Get number of pole groups
            n_pole_groups = len(self.poles)

            # Create empty lists
            residues_modified_all = [None] * n_pole_groups
            constant_modified_all = [None] * n_pole_groups

            # Create residues_modified and constant_modified for all pole groups
            for idx_pole_group in range(n_pole_groups):
                residues_modified_all[idx_pole_group], constant_modified_all[idx_pole_group] = \
                    self._get_residues_and_constant_modified(
                        self.poles[idx_pole_group],
                        self.residues[idx_pole_group],
                        self.constant[idx_pole_group])

            # Update residues and constant
            for i in range(n_ports):
                for j in range(n_ports):
                    idx_response = i * n_ports + j
                    idx_pole_group = self.map_idx_response_to_idx_pole_group[idx_response]
                    idx_pole_group_member = self.map_idx_response_to_idx_pole_group_member[idx_response]
                    residues = self.residues[idx_pole_group][idx_pole_group_member]
                    poles = self.poles[idx_pole_group]
                    constant = self.constant[idx_pole_group][idx_pole_group_member]
                    constant_modified = constant_modified_all[idx_pole_group][idx_pole_group_member]
                    # Initialize constant to constant_modified (dc value only)
                    constant = constant_modified
                    idx_column_Ct = 0
                    C_modified_response = C_view[i][j]
                    # Update residues
                    for idx_residue, residue in enumerate(residues):
                        if np.imag(residue) == 0.0:
                            # Real residue
                            residues[idx_residue] = C_modified_response[idx_column_Ct] * poles[idx_residue]
                            constant += np.real(C_modified_response[idx_column_Ct])
                            idx_column_Ct += 1
                        else:
                            # Complex-conjugate residue
                            residues[idx_residue] = \
                                (C_modified_response[idx_column_Ct] + 1j * C_modified_response[idx_column_Ct + 1]) * \
                                    poles[idx_residue]
                            constant += 2 * np.real(C_modified_response[idx_column_Ct])
                            idx_column_Ct += 2
                    # Update constant
                    self.constant[idx_pole_group][idx_pole_group_member] = constant
        else:
            # Update residues
            for i in range(n_ports):
                for j in range(n_ports):
                    idx_response = i * n_ports + j
                    idx_pole_group = self.map_idx_response_to_idx_pole_group[idx_response]
                    idx_pole_group_member = self.map_idx_response_to_idx_pole_group_member[idx_response]
                    residues = self.residues[idx_pole_group][idx_pole_group_member]
                    idx_column_Ct = 0
                    C_response = C_view[i][j]
                    for idx_residue, residue in enumerate(residues):
                        if np.imag(residue) == 0.0:
                            # Real residue
                            residues[idx_residue] = C_response[idx_column_Ct]
                            idx_column_Ct += 1
                        else:
                            # Complex-conjugate residue
                            residues[idx_residue] = \
                                C_response[idx_column_Ct] + 1j * C_response[idx_column_Ct + 1]
                            idx_column_Ct += 2

            # Update constant
            if have_D and perturb_constant:
                for i in range(n_ports):
                    for j in range(n_ports):
                        idx_response = i * n_ports + j
                        idx_pole_group = self.map_idx_response_to_idx_pole_group[idx_response]
                        idx_pole_group_member = self.map_idx_response_to_idx_pole_group_member[idx_response]
                        self.constant[idx_pole_group][idx_pole_group_member] = D[i, j]

    def write_npz(self, path: str) -> None:
        """
        Writes the model parameters in :attr:`poles`, :attr:`residues`,
        :attr:`proportional` and :attr:`constant` to a labeled NumPy .npz file.

        Parameters
        ----------
        path : str
            Target path without filename for the export. The filename will be added automatically based on the network
            name in :attr:`network`

        Returns
        -------
        None

        See Also
        --------
        read_npz : Reads all model parameters from a .npz file

        Examples
        --------
        Load and fit the `Network`, then export the model parameters to a .npz file:

        >>> nw_3port = skrf.Network('my3port.s3p')
        >>> vf = skrf.VectorFitting(nw_3port)
        >>> vf.vector_fit(n_poles_real=1, n_poles_cmplx=4)
        >>> vf.write_npz('./data/')

        The filename depends on the network name stored in `nw_3port.name` and will have the prefix `coefficients_`, for
        example `coefficients_my3port.npz`. The coefficients can then be read using NumPy's load() function:

        >>> coeffs = numpy.load('./data/coefficients_my3port.npz')
        >>> poles = coeffs['poles']
        >>> residues = coeffs['residues']
        >>> prop_coeffs = coeffs['proportionals']
        >>> constants = coeffs['constants']

        Alternatively, the coefficients can be read directly into a new instance of `VectorFitting`, see
        :func:`read_npz`.
        """

        if self.poles is None:
            warnings.warn('Nothing to export; Poles have not been fitted.', RuntimeWarning, stacklevel=2)
            return
        if self.residues is None:
            warnings.warn('Nothing to export; Residues have not been fitted.', RuntimeWarning, stacklevel=2)
            return
        if self.proportional is None:
            warnings.warn('Nothing to export; Proportional coefficients have not been fitted.', RuntimeWarning,
                          stacklevel=2)
            return
        if self.constant is None:
            warnings.warn('Nothing to export; Constants have not been fitted.', RuntimeWarning, stacklevel=2)
            return

        filename = self.network.name
        path=os.path.join(path, f'{filename}_model')
        logger.info(f'Exporting results as compressed NumPy array to {path}.npz')

        # Initialize the save dictionary
        save_dict = {}

        # Helper function to handle numpy arrays or lists of numpy arrays
        def process_data(key, data):
            if isinstance(data, list):
                for idx, item in enumerate(data):
                    save_dict[f"{key}{idx}"] = item
                save_dict[f"n_{key}"] = len(data)  # Save the number of list elements
            else:
                save_dict[key] = data

        # Process each attribute
        process_data("poles", self.poles)
        process_data("residues", self.residues)
        process_data("proportional", self.proportional)
        process_data("constant", self.constant)
        process_data("map_idx_response_to_idx_pole_group", self.map_idx_response_to_idx_pole_group)
        process_data("map_idx_response_to_idx_pole_group_member", self.map_idx_response_to_idx_pole_group_member)

        # Save the data
        np.savez_compressed(path, **save_dict)

    def read_npz(self, file: str) -> None:
        """
        Reads all model parameters :attr:`poles`, :attr:`residues`, :attr:`proportional` and
        :attr:`constant` from a labeled NumPy .npz file.

        Parameters
        ----------
        file : str
            NumPy .npz file containing the parameters. See notes.

        Returns
        -------
        None

        Raises
        ------
        ValueError
            If the shapes of the coefficient arrays in the provided file are not compatible.

        Notes
        -----
        The .npz file needs to include the model parameters as individual NumPy arrays (ndarray) labeled '*poles*',
        '*residues*', '*proportionals*' and '*constants*'. The shapes of those arrays need to match the network
        properties in :class:`network` (correct number of ports). Preferably, the .npz file was created by
        :func:`write_npz`.

        See Also
        --------
        write_npz : Writes all model parameters to a .npz file

        Examples
        --------
        Create an empty `VectorFitting` instance (with or without the fitted `Network`) and load the model parameters:

        >>> vf = skrf.VectorFitting(None)
        >>> vf.read_npz('./data/coefficients_my3port.npz')

        This can be useful to analyze or process a previous vector fit instead of fitting it again, which sometimes
        takes a long time. For example, the model passivity can be evaluated and enforced:

        >>> vf.passivity_enforce()
        """


        # Load the data
        data = np.load(file)

        # Helper function to reconstruct numpy arrays or lists of numpy arrays
        def reconstruct_data(key):
            if f"n_{key}" in data:
                n_items = int(data[f"n_{key}"])
                return [data[f"{key}{i}"] for i in range(n_items)]
            return data[key]

        # Reconstruct each attribute
        self.poles = reconstruct_data("poles")
        self.proportional = reconstruct_data("proportional")
        self.constant = reconstruct_data("constant")
        self.residues = reconstruct_data("residues")
        self.map_idx_response_to_idx_pole_group = reconstruct_data("map_idx_response_to_idx_pole_group")
        self.map_idx_response_to_idx_pole_group_member = reconstruct_data("map_idx_response_to_idx_pole_group_member")

    def get_model_response(self, i: int, j: int, freqs: Any = None) -> np.ndarray:
        """
        Returns one of the frequency responses :math:`H_{i+1,j+1}` of the fitted model :math:`H`.

        Parameters
        ----------
        i : int
            Row index of the response in the response matrix.

        j : int
            Column index of the response in the response matrix.

        freqs : list of float or ndarray or None, optional
            List of frequencies for the response plot. If None, the sample frequencies of the fitted network in
            :attr:`network` are used.

        Returns
        -------
        response : ndarray
            Model response :math:`H_{i+1,j+1}` at the frequencies specified in `freqs` (complex-valued Numpy array).

        Examples
        --------
        Get fitted S11 at 101 frequencies from 0 Hz to 10 GHz:

        >>> import skrf
        >>> vf = skrf.VectorFitting(skrf.data.ring_slot)
        >>> vf.vector_fit(3, 0)
        >>> s11_fit = vf.get_model_response(0, 0, numpy.linspace(0, 10e9, 101))
        """

        if self.poles is None:
            warnings.warn('Returning a zero-vector; Poles have not been fitted.',
                          RuntimeWarning, stacklevel=2)
            return np.zeros_like(freqs)
        if self.residues is None:
            warnings.warn('Returning a zero-vector; Residues have not been fitted.',
                          RuntimeWarning, stacklevel=2)
            return np.zeros_like(freqs)
        if self.proportional is None:
            warnings.warn('Returning a zero-vector; Proportional coefficients have not been fitted.',
                          RuntimeWarning, stacklevel=2)
            return np.zeros_like(freqs)
        if self.constant is None:
            warnings.warn('Returning a zero-vector; Constants have not been fitted.',
                          RuntimeWarning, stacklevel=2)
            return np.zeros_like(freqs)
        if freqs is None:
            freqs = np.linspace(np.amin(self.network.f), np.amax(self.network.f), 1000)

        s = 2j * np.pi * np.array(freqs)
        n_ports = self._get_n_ports()
        idx_response = i * n_ports + j

        # Get pole group index
        idx_pole_group=self.map_idx_response_to_idx_pole_group[idx_response]

        # Get pole group member index
        idx_pole_group_member=self.map_idx_response_to_idx_pole_group_member[idx_response]

        # Get data
        poles = self.poles[idx_pole_group]
        residues = self.residues[idx_pole_group][idx_pole_group_member]
        constant = self.constant[idx_pole_group][idx_pole_group_member]
        proportional = self.proportional[idx_pole_group][idx_pole_group_member]

        # Calculate model_response
        model_response = proportional * s + constant

        for i, pole in enumerate(poles):
            if np.imag(pole) == 0.0:
                # real pole
                model_response += residues[i] / (s - pole)
            else:
                # complex conjugate pole
                model_response += residues[i] / (s - pole) + np.conjugate(residues[i]) / (s - np.conjugate(pole))

        return model_response

    def plot_model_vs_data(self):
        # Plot fit vs original data only in the data frequency range
        freqs = np.linspace(np.min(self.network.f), np.max(self.network.f), 1001)
        n_ports=self._get_n_ports()
        fig, ax = mplt.subplots(n_ports, n_ports)
        fig.set_size_inches(12, 8)
        for i in range(n_ports):
            for j in range(n_ports):
                self.plot_s_db(i, j, freqs=freqs, ax=ax[i][j])
        fig.tight_layout()
        mplt.show()

        # Plot fit vs original data up to 10 times the maximum data frequency
        freqs = np.linspace(np.min(self.network.f), 10*np.max(self.network.f), 1001)
        n_ports=self._get_n_ports()
        fig, ax = mplt.subplots(n_ports, n_ports)
        fig.set_size_inches(12, 8)
        for i in range(n_ports):
            for j in range(n_ports):
                self.plot_s_db(i, j, freqs=freqs, ax=ax[i][j])
        fig.tight_layout()
        mplt.show()

        # Plot fit vs original data up to 100 times the maximum data frequency
        freqs = np.linspace(np.min(self.network.f), 100*np.max(self.network.f), 1001)
        n_ports=self._get_n_ports()
        fig, ax = mplt.subplots(n_ports, n_ports)
        fig.set_size_inches(12, 8)
        for i in range(n_ports):
            for j in range(n_ports):
                self.plot_s_db(i, j, freqs=freqs, ax=ax[i][j])
        fig.tight_layout()
        mplt.show()


    @axes_kwarg
    def plot(self, component: str, i: int = -1, j: int = -1, freqs: Any = None,
             parameter: str = 's', *, ax: Axes = None) -> Axes:
        """
        Plots the specified component of the parameter :math:`H_{i+1,j+1}` in the fit, where :math:`H` is
        either the scattering (:math:`S`), the impedance (:math:`Z`), or the admittance (:math:`H`) response specified
        in `parameter`.

        Parameters
        ----------
        component : str
            The component to be plotted. Must be one of the following items:
            ['db', 'mag', 'deg', 'deg_unwrap', 're', 'im'].
            `db` for magnitude in decibels,
            `mag` for magnitude in linear scale,
            `deg` for phase in degrees (wrapped),
            `deg_unwrap` for phase in degrees (unwrapped/continuous),
            `re` for real part in linear scale,
            `im` for imaginary part in linear scale.

        i : int, optional
            Row index of the response. `-1` to plot all rows.

        j : int, optional
            Column index of the response. `-1` to plot all columns.

        freqs : list of float or ndarray or None, optional
            List of frequencies for the response plot. If None, the sample frequencies of the fitted network in
            :attr:`network` are used. This only works if :attr:`network` is not `None`.

        parameter : str, optional
            The network representation to be used. This is only relevant for the plot of the original sampled response
            in :attr:`network` that is used for comparison with the fit. Must be one of the following items unless
            :attr:`network` is `None`: ['s', 'z', 'y'] for *scattering* (default), *impedance*, or *admittance*.

        ax : :class:`matplotlib.Axes` object or None
            matplotlib axes to draw on. If None, the current axes is fetched with :func:`gca()`.

        Returns
        -------
        :class:`matplotlib.Axes`
            matplotlib axes used for drawing. Either the passed :attr:`ax` argument or the one fetch from the current
            figure.

        Raises
        ------
        ValueError
            If the `freqs` parameter is not specified while the Network in :attr:`network` is `None`.
            Also if `component` and/or `parameter` are not valid.
        """

        components = ['db', 'mag', 'deg', 'deg_unwrap', 're', 'im', 'rel_err', 'abs_err']

        # Convert to lower case
        component=component.lower()
        parameter=parameter.lower()

        if component == 'rel_err' or component == 'abs_err':
            plot_error=True

            # For error plots the frequency can't be specified because
            # we only have the frequency of the samples.
            freqs = self.network.f
        else:
            plot_error=False

        if component in components:
            if self.residues is None or self.poles is None:
                raise RuntimeError('Poles and/or residues have not been fitted. Cannot plot the model response.')

            n_ports = self._get_n_ports()

            if i == -1:
                list_i = range(n_ports)
            elif isinstance(i, int):
                list_i = [i]
            else:
                list_i = i

            if j == -1:
                list_j = range(n_ports)
            elif isinstance(j, int):
                list_j = [j]
            else:
                list_j = j

            if self.network is not None and not plot_error:
                # Plot the original network response at each sample frequency (scatter plot)
                responses = self._get_responses_from_network(parameter)

                i_samples = 0
                for i in list_i:
                    for j in list_j:
                        if i_samples == 0:
                            label = 'Samples'
                        else:
                            label = '_nolegend_'
                        i_samples += 1

                        y_vals = None
                        if component == 'db':
                            y_vals = 20 * np.log10(np.abs(responses[:, i, j]))
                        elif component == 'mag':
                            y_vals = np.abs(responses[:, i, j])
                        elif component == 'deg':
                            y_vals = np.rad2deg(np.angle(responses[:, i, j]))
                        elif component == 'deg_unwrap':
                            y_vals = np.rad2deg(np.unwrap(np.angle(responses[:, i, j])))
                        elif component == 're':
                            y_vals = np.real(responses[:, i, j])
                        elif component == 'im':
                            y_vals = np.imag(responses[:, i, j])

                        ax.scatter(self.network.f[:], y_vals[:], color='r', label=label)

                if freqs is None:
                    # get frequency array from the network
                    freqs = self.network.f

            if freqs is None:
                raise ValueError(
                    'Neither `freqs` nor `self.network` is specified. Cannot plot model response without any '
                    'frequency information.')

            # Plot the fitted responses or errors
            y_label = ''
            i_fit = 0

            for i in list_i:
                for j in list_j:
                    if i_fit == 0:
                        label = 'Fit'
                    else:
                        label = '_nolegend_'
                    i_fit += 1

                    if component == 'rel_err':
                        y_vals = self.get_rel_error(i, j)
                        y_label = 'Rel. Error'

                    elif component == 'abs_err':
                        y_vals = self.get_abs_error(i, j)
                        y_label = 'Abs. Error'

                    else:
                        y_model = self.get_model_response(i, j, freqs)

                        y_vals = None
                        if component == 'db':
                            y_vals = 20 * np.log10(np.abs(y_model))
                            y_label = 'Magnitude (dB)'
                        elif component == 'mag':
                            y_vals = np.abs(y_model)
                            y_label = 'Magnitude'
                        elif component == 'deg':
                            y_vals = np.rad2deg(np.angle(y_model))
                            y_label = 'Phase (Degrees)'
                        elif component == 'deg_unwrap':
                            y_vals = np.rad2deg(np.unwrap(np.angle(y_model)))
                            y_label = 'Phase (Degrees)'
                        elif component == 're':
                            y_vals = np.real(y_model)
                            y_label = 'Real Part'
                        elif component == 'im':
                            y_vals = np.imag(y_model)
                            y_label = 'Imaginary Part'

                    ax.plot(freqs, y_vals, color='k', label=label)

            ax.set_xlabel('Frequency (Hz)')
            ax.set_ylabel(y_label)
            ax.legend(loc='best')

            # Only print title if a single response is shown
            if i_fit == 1:
                ax.set_title(f'Response i={i}, j={j}')

            return ax
        else:
            raise ValueError(f'The specified component ("{component}") is not valid. Must be in {components}.')

    def plot_abs_err(self, *args, **kwargs) -> Axes:
        """
        Plots the absolute error of the fit

        Parameters
        ----------
        *args : any, optional
            Additonal arguments to be passed to :func:`plot`.

        **kwargs : dict, optional
            Additonal keyword arguments to be passed to :func:`plot`.

        Returns
        -------
        :class:`matplotlib.Axes`
            matplotlib axes used for drawing. Either the passed :attr:`ax` argument or the one fetch from the current
            figure.

        Notes
        -----
        This simply calls ``plot('abs_err', *args, **kwargs)``.
        """

        return self.plot('abs_err', *args, **kwargs)

    def plot_rel_err(self, *args, **kwargs) -> Axes:
       """
       Plots the relative error of the fit

       Parameters
       ----------
       *args : any, optional
           Additonal arguments to be passed to :func:`plot`.

       **kwargs : dict, optional
           Additonal keyword arguments to be passed to :func:`plot`.

       Returns
       -------
       :class:`matplotlib.Axes`
           matplotlib axes used for drawing. Either the passed :attr:`ax` argument or the one fetch from the current
           figure.

       Notes
       -----
       This simply calls ``plot('rel_err', *args, **kwargs)``.
       """

       return self.plot('rel_err', *args, **kwargs)

    def plot_s_db(self, *args, **kwargs) -> Axes:
        """
        Plots the magnitude in dB of the scattering parameter response(s) in the fit.

        Parameters
        ----------
        *args : any, optional
            Additonal arguments to be passed to :func:`plot`.

        **kwargs : dict, optional
            Additonal keyword arguments to be passed to :func:`plot`.

        Returns
        -------
        :class:`matplotlib.Axes`
            matplotlib axes used for drawing. Either the passed :attr:`ax` argument or the one fetch from the current
            figure.

        Notes
        -----
        This simply calls ``plot('db', *args, **kwargs)``.
        """

        return self.plot('db', *args, **kwargs)

    def plot_s_mag(self, *args, **kwargs) -> Axes:
        """
        Plots the magnitude in linear scale of the scattering parameter response(s) in the fit.

        Parameters
        ----------
        *args : any, optional
            Additonal arguments to be passed to :func:`plot`.

        **kwargs : dict, optional
            Additonal keyword arguments to be passed to :func:`plot`.

        Returns
        -------
        :class:`matplotlib.Axes`
            matplotlib axes used for drawing. Either the passed :attr:`ax` argument or the one fetch from the current
            figure.

        Notes
        -----
        This simply calls ``plot('mag', *args, **kwargs)``.
        """

        return self.plot('mag', *args, **kwargs)

    def plot_s_deg(self, *args, **kwargs) -> Axes:
        """
        Plots the phase in degrees of the scattering parameter response(s) in the fit.

        Parameters
        ----------
        *args : any, optional
            Additonal arguments to be passed to :func:`plot`.

        **kwargs : dict, optional
            Additonal keyword arguments to be passed to :func:`plot`.

        Returns
        -------
        :class:`matplotlib.Axes`
            matplotlib axes used for drawing. Either the passed :attr:`ax` argument or the one fetch from the current
            figure.

        Notes
        -----
        This simply calls ``plot('deg', *args, **kwargs)``.
        """

        return self.plot('deg', *args, **kwargs)

    def plot_s_deg_unwrap(self, *args, **kwargs) -> Axes:
        """
        Plots the unwrapped phase in degrees of the scattering parameter response(s) in the fit.

        Parameters
        ----------
        *args : any, optional
            Additonal arguments to be passed to :func:`plot`.

        **kwargs : dict, optional
            Additonal keyword arguments to be passed to :func:`plot`.

        Returns
        -------
        :class:`matplotlib.Axes`
            matplotlib axes used for drawing. Either the passed :attr:`ax` argument or the one fetch from the current
            figure.

        Notes
        -----
        This simply calls ``plot('deg_unwrap', *args, **kwargs)``.
        """

        return self.plot('deg_unwrap', *args, **kwargs)

    def plot_s_re(self, *args, **kwargs) -> Axes:
        """
        Plots the real part of the scattering parameter response(s) in the fit.

        Parameters
        ----------
        *args : any, optional
            Additonal arguments to be passed to :func:`plot`.

        **kwargs : dict, optional
            Additonal keyword arguments to be passed to :func:`plot`.

        Returns
        -------
        :class:`matplotlib.Axes`
            matplotlib axes used for drawing. Either the passed :attr:`ax` argument or the one fetch from the current
            figure.

        Notes
        -----
        This simply calls ``plot('re', *args, **kwargs)``.
        """

        return self.plot('re', *args, **kwargs)

    def plot_s_im(self, *args, **kwargs) -> Axes:
        """
        Plots the imaginary part of the scattering parameter response(s) in the fit.

        Parameters
        ----------
        *args : any, optional
            Additonal arguments to be passed to :func:`plot`.

        **kwargs : dict, optional
            Additonal keyword arguments to be passed to :func:`plot`.

        Returns
        -------
        :class:`matplotlib.Axes`
            matplotlib axes used for drawing. Either the passed :attr:`ax` argument or the one fetch from the current
            figure.

        Notes
        -----
        This simply calls ``plot('im', *args, **kwargs)``.
        """

        return self.plot('im', *args, **kwargs)

    @axes_kwarg
    def plot_s_singular(self, freqs: Any = None, *, ax: Axes = None) -> Axes:
        """
        Plots the singular values of the vector fitted S-matrix in linear scale.

        Parameters
        ----------
        freqs : list of float or ndarray or None, optional
            List of frequencies for the response plot. If None, the sample frequencies of the fitted network in
            :attr:`network` are used. This only works if :attr:`network` is not `None`.

        ax : :class:`matplotlib.Axes` object or None
            matplotlib axes to draw on. If None, the current axes is fetched with :func:`gca()`.

        i, j: integers, required if shared_poles==False. I this case, i and j are the indices
            for which response the singular values are plotted

        Returns
        -------
        :class:`matplotlib.Axes`
            matplotlib axes used for drawing. Either the passed :attr:`ax` argument or the one fetch from the current
            figure.

        Raises
        ------
        ValueError
            If the `freqs` parameter is not specified while the Network in :attr:`network` is `None`.
        """

        if freqs is None:
            if self.network is None:
                raise ValueError(
                    'Neither `freqs` nor `self.network` is specified. Cannot plot model response without any '
                    'frequency information.')
            else:
                freqs = self.network.f

        # Calculate s
        s = 2j * np.pi * freqs

        # Get n_ports
        n_ports = self._get_n_ports()

        # Calculate singular values for each frequency
        u, sigma, vh = np.linalg.svd(self._get_S_from_model(s))

        # Plot the frequency response of each singular value
        for n in range(n_ports):
            ax.plot(freqs, sigma[:, n], label=fr'$\sigma$ idx={n + 1}')
        ax.set_xlabel('Frequency (Hz)')
        ax.set_ylabel('Magnitude')
        ax.legend(loc='best')

        # Add a horizontal line at y=1
        ax.plot(freqs, np.ones(np.size(freqs, axis=0)), color='black')

        return ax

    @axes_kwarg
    def plot_convergence(self, ax: Axes = None) -> Axes:
        """
        Plots the history of the model residue parameter **d_tilde** during the iterative pole relocation process of the
        vector fitting, which should eventually converge to a fixed value. Additionally, the relative change of the
        maximum singular value of the coefficient matrix **A** are plotted, which serve as a convergence indicator.

        Parameters
        ----------
        ax : :class:`matplotlib.Axes` object or None
            matplotlib axes to draw on. If None, the current axes is fetched with :func:`gca()`.

        Returns
        -------
        :class:`matplotlib.Axes`
            matplotlib axes used for drawing. Either the passed :attr:`ax` argument or the one fetch from the current
            figure.
        """

        ax.semilogy(np.arange(len(self.delta_rel_max_singular_value_A_dense_history)) + 1,
                    self.delta_rel_max_singular_value_A_dense_history, color='darkblue')
        ax.set_xlabel('Iteration step')
        ax.set_ylabel('Max. relative change', color='darkblue')
        ax2 = ax.twinx()
        ax2.plot(np.arange(len(self.d_tilde_history)) + 1, self.d_tilde_history, color='orangered')
        ax2.set_ylabel('Residue', color='orangered')
        return ax

    @axes_kwarg
    def plot_passivation(self, ax: Axes = None) -> Axes:
        """
        Plots the history of the greatest singular value during the iterative passivity enforcement process, which
        should eventually converge to a value slightly lower than 1.0 or stop after reaching the maximum number of
        iterations specified in the class variable :attr:`max_iterations`.

        Parameters
        ----------
        ax : :class:`matplotlib.Axes` object or None
            matplotlib axes to draw on. If None, the current axes is fetched with :func:`gca()`.

        Returns
        -------
        :class:`matplotlib.Axes`
            matplotlib axes used for drawing. Either the passed :attr:`ax` argument or the one fetch from the current
            figure.
        """

        ax.plot(np.arange(len(self.history_max_sigma)) + 1, self.history_max_sigma)
        ax.set_xlabel('Iteration step')
        ax.set_ylabel('Max. singular value')
        return ax

    def write_spice_subcircuit_s(self, file: str, fitted_model_name: str = "s_equivalent",
                                     create_reference_pins: bool = False,
                                     topology: str = 'impedance_v2a') -> None:
        """
        Creates an equivalent N-port subcircuit based on its vector fitted S parameter responses
        in spice simulator netlist syntax (compatible with ngspice, Xyce, ...).

        Parameters
        ----------
        file : str
            Path and filename including file extension (usually .sNp) for the subcircuit file.

        fitted_model_name: str
            Name of the resulting subcircuit, default "s_equivalent"

        create_reference_pins: bool
            If set to True, the synthesized subcircuit will have N pin-pairs:
            p1 p1_ref p2 p2_ref ... pN pN_ref

            If set to False, the synthesized subcircuit will have N pins
            p1 p2 ... pN
            In this case, the reference nodes will be internally connected
            to the global ground net 0.

            The default is False

        Returns
        -------
        None

        Examples
        --------
        Load and fit the `Network`, then export the equivalent subcircuit:

        >>> nw_3port = skrf.Network('my3port.s3p')
        >>> vf = skrf.VectorFitting(nw_3port)
        >>> vf.auto_fit()
        >>> vf.write_spice_subcircuit_s('/my3port_model.sp')

        References
        ----------
        .. [1] G. Antonini, "SPICE Equivalent Circuits of Frequency-Domain Responses", IEEE Transactions on
            Electromagnetic Compatibility, vol. 45, no. 3, pp. 502-512, August 2003,
            doi: https://doi.org/10.1109/TEMC.2003.815528

        .. [2] C. -C. Chou and J. E. Schutt-Ainé, "Equivalent Circuit Synthesis of Multiport S Parameters in
            Pole–Residue Form," in IEEE Transactions on Components, Packaging and Manufacturing Technology,
            vol. 11, no. 11, pp. 1971-1979, Nov. 2021, doi: 10.1109/TCPMT.2021.3115113

        .. [3] Romano D, Antonini G, Grossner U, Kovačević-Badstübner I. Circuit synthesis techniques of
            rational models of electromagnetic systems: A tutorial paper. Int J Numer Model. 2019
            doi: https://doi.org/10.1002/jnm.2612

        """
        if topology == 'impedance_v1a':
            self._write_spice_subcircuit_s_impedance_v1a(file, fitted_model_name, create_reference_pins)
        elif topology == 'impedance_v1b':
            self._write_spice_subcircuit_s_impedance_v1b(file, fitted_model_name, create_reference_pins)
        elif topology == 'impedance_v2a':
            self._write_spice_subcircuit_s_impedance_v2a(file, fitted_model_name, create_reference_pins)
        elif topology == 'impedance_v2b':
            self._write_spice_subcircuit_s_impedance_v2a(file, fitted_model_name, create_reference_pins)
        elif topology == 'admittance_v1':
            self._write_spice_subcircuit_s_admittance_v1(file, fitted_model_name, create_reference_pins)
        elif topology == 'admittance_v2':
            self._write_spice_subcircuit_s_admittance_v2(file, fitted_model_name, create_reference_pins)
        else:
            warnings.warn('Invalid choice of topology. Proceeding with impedance_v2',
                          UserWarning, stacklevel=2)
            self._write_spice_subcircuit_s_impedance_v2(file, fitted_model_name, create_reference_pins)

        print(f'Wrote netlist to {file} using topology {topology}')

    def _write_spice_subcircuit_s_impedance_v1a(self, file: str, fitted_model_name: str = "s_equivalent",
                                     create_reference_pins: bool = False) -> None:
        # This version has only two G sources to transfer the reflected wave b to the ports.
        # I would have guessed that it would be faster than having a G source for every pole but it is
        # indeed about 20 % slower in the linear solve. In transient it's the same speed.
        # This version writes out positive or negative R values without using G-Sources parallel to them if their
        # value is negative.

        """
        Creates an equivalent N-port subcircuit based on its vector fitted S parameter responses
        in spice simulator netlist syntax (compatible with ngspice, Xyce, ...).

        Parameters
        ----------
        file : str
            Path and filename including file extension (usually .sNp) for the subcircuit file.

        fitted_model_name: str
            Name of the resulting subcircuit, default "s_equivalent"

        create_reference_pins: bool
            If set to True, the synthesized subcircuit will have N pin-pairs:
            p1 p1_ref p2 p2_ref ... pN pN_ref

            If set to False, the synthesized subcircuit will have N pins
            p1 p2 ... pN
            In this case, the reference nodes will be internally connected
            to the global ground net 0.

            The default is False

        Returns
        -------
        None

        Examples
        --------
        Load and fit the `Network`, then export the equivalent subcircuit:

        >>> nw_3port = skrf.Network('my3port.s3p')
        >>> vf = skrf.VectorFitting(nw_3port)
        >>> vf.auto_fit()
        >>> vf.write_spice_subcircuit_s('/my3port_model.sp')

        References
        ----------
        .. [1] G. Antonini, "SPICE Equivalent Circuits of Frequency-Domain Responses", IEEE Transactions on
            Electromagnetic Compatibility, vol. 45, no. 3, pp. 502-512, August 2003,
            doi: https://doi.org/10.1109/TEMC.2003.815528

        .. [2] C. -C. Chou and J. E. Schutt-Ainé, "Equivalent Circuit Synthesis of Multiport S Parameters in
            Pole–Residue Form," in IEEE Transactions on Components, Packaging and Manufacturing Technology,
            vol. 11, no. 11, pp. 1971-1979, Nov. 2021, doi: 10.1109/TCPMT.2021.3115113

        .. [3] Romano D, Antonini G, Grossner U, Kovačević-Badstübner I. Circuit synthesis techniques of
            rational models of electromagnetic systems: A tutorial paper. Int J Numer Model. 2019
            doi: https://doi.org/10.1002/jnm.2612

        """

        with open(file, 'w') as f:
            netlist_header = self._get_netlist_header(create_reference_pins=create_reference_pins,
                                                      fitted_model_name=fitted_model_name)
            f.write(netlist_header)

            # Write title line
            f.write('* EQUIVALENT CIRCUIT FOR VECTOR FITTED S-MATRIX\n')
            f.write('* Created using scikit-rf vectorFitting.py\n')
            f.write('*\n')

            # Create subcircuit pin string and reference nodes
            if create_reference_pins:
                str_input_nodes = " ".join(map(lambda x: f'p{x + 1} p{x + 1}_ref', range(self.network.nports)))
            else:
                str_input_nodes = " ".join(map(lambda x: f'p{x + 1}', range(self.network.nports)))

            f.write(f'.SUBCKT {fitted_model_name} {str_input_nodes}\n')

            for i in range(self.network.nports):
                f.write('*\n')
                f.write(f'* Port network for port {i + 1}\n')

                if create_reference_pins:
                    node_ref_i = f'p{i + 1}_ref'
                else:
                    node_ref_i = '0'

                # Reference impedance (real, i.e. resistance) of port i
                z0_i = np.real(self.network.z0[0, i])

                # Dummy voltage source (v = 0) for port current sensing (I_i)
                f.write(f'V{i + 1} p{i + 1} s{i + 1} 0\n')

                # Port reference impedance Z0_i
                f.write(f'R{i + 1} s{i + 1} {node_ref_i} {z0_i}\n')

                # Initialize node counters for a_i (p) and -a_i (n)
                n_current_pos = 0
                n_current_neg = 0
                node_pos = '0'
                node_neg = '0'

                for j in range(self.network.nports):
                    # Stacking order in VectorFitting class variables:
                    # s11, s12, s13, ..., s21, s22, s23, ...
                    i_response = j * self.network.nports + i

                    # Get idx_pole_group and idx_pole_group_member for current response
                    idx_pole_group=self.map_idx_response_to_idx_pole_group[i_response]
                    idx_pole_group_member=self.map_idx_response_to_idx_pole_group_member[i_response]

                    # Get residues
                    residues = self.residues[idx_pole_group][idx_pole_group_member]

                    # Get poles
                    poles=self.poles[idx_pole_group]

                    f.write('*\n')
                    f.write(f'* Transfer from port {i + 1} to port {j + 1}\n')

                    # Reference impedance (real, i.e. resistance) of port j
                    z0_j = np.real(self.network.z0[0, j])

                    if create_reference_pins:
                        node_ref_j = f'p{j + 1}_ref'
                    else:
                        node_ref_j = '0'

                    # Get proportional and constant term of the model
                    d = self.constant[idx_pole_group][idx_pole_group_member]
                    e = self.proportional[idx_pole_group][idx_pole_group_member]

                    # Store begin nodes of series impedance chains
                    node_pos_begin = node_pos
                    node_neg_begin = node_neg

                    # R for constant term
                    if d != 0.0:
                        # Calculated resistence can be negative, but implementation must use positive values.
                        # Append to pos or neg impedance chain depending on sign
                        if d < 0:
                            n_current_neg += 1
                            node1 = node_neg
                            node2 = node_neg = f'n_a{i + 1}_n_{n_current_neg}'

                        else:
                            n_current_pos += 1
                            node1 = node_pos
                            node2 = node_pos = f'n_a{i + 1}_p_{n_current_pos}'

                        f.write(f'R{j + 1}_{i + 1} {node1} {node2} {np.abs(d)}\n')

                    # L for proportional term
                    if e != 0.0:
                        # Append to pos or neg impedance chain depending on sign
                        if d < 0:
                            n_current_neg += 1
                            node1 = node_neg
                            node2 = node_neg = f'n_a{i + 1}_n_{n_current_neg}'
                        else:
                            n_current_pos += 1
                            node1 = node_pos
                            node2 = node_pos = f'n_a{i + 1}_p_{n_current_pos}'

                        f.write(f'L{j + 1}_{i + 1} {node1} {node2} {np.abs(e)}\n')

                    # Transfer admittances represented by poles and residues
                    for idx_pole in range(len(poles)):
                        pole = poles[idx_pole]
                        residue = residues[idx_pole]

                        # Calculated component values can be negative, but implementation must use positive values.
                        # The sign of the residue can be inverted, but then the inversion must be compensated by
                        # flipping the polarity of the VCCS control voltage
                        if np.real(residue) < 0.0:
                            # Residue multiplication with -1 required
                            residue = -1 * residue
                            n_current_neg += 1
                            node1 = node_neg
                            node2 = node_neg = f'n_a{i + 1}_n_{n_current_neg}'
                        else:
                            n_current_pos += 1
                            node1 = node_pos
                            node2 = node_pos = f'n_a{i + 1}_p_{n_current_pos}'

                        # Impedance representing S_j_i_k
                        if np.imag(pole) == 0.0:
                            # Real pole; Add parallel RC network via `rc_passive`
                            c = 1 / np.real(residue)
                            r = -1 * np.real(residue) / np.real(pole)
                            f.write(f'X{j + 1}_{i + 1}_{idx_pole} {node1} {node2} rc_passive res={r} cap={c}\n')
                        else:
                            # Complex pole of a conjugate pair; Add active or passive RCL network via `rcl_active`

                            # Calculation of the values for r1, r2, l and c using the transfer function coefficients
                            # and comparing them with the coefficients of the generic transfer function of a complex
                            # conjugated pole pair (see Antonini paper) gives the equations eq1..eq4.
                            #
                            # Transfer function of r1+sl parallel to c parallel to r2:
                            # H(s) = (s/c + r1/(lc)) / (s**2 + s(r1/l + 1/(r2 c)) + (r1/(r2 l c) + 1/(lc)))
                            #
                            # From Antonini:
                            # H'(s) = (2 cre s - 2 (cre pre + cim pim)) / (s**2 - 2 pre s + abs(p)**2)
                            #
                            # Using these abbreviations in the code:
                            # cre=np.real(residue)
                            # cim=np.imag(residue)
                            # pre=np.real(pole)
                            # pim=np.imag(pole)
                            #
                            # from sympy import symbols, Eq, solve, re, im, Abs, simplify, ask, Q, printing
                            #
                            # # Define symbols
                            # r1, r2 = symbols('r1 r2', real=True)
                            # c, l = symbols('c l', real=True, positive=True)
                            # cre = symbols('cre', real=True, positive=True)
                            # cim, pre, pim = symbols('cim pre pim', real=True)
                            #
                            # # Equations from coefficient comparison:
                            # eq1 = Eq(1 / c, 2 * cre)
                            # eq2 = Eq(r1 / (l * c), -2 * (cre * pre + cim * pim))
                            # eq3 = Eq(r1 / l + 1 / (r2 * c), -2 * pre)
                            # eq4 = Eq(r1 / (r2 * l * c) + 1 / (l * c), Abs(pre + 1j*pim)**2)
                            #
                            # # Solve system of equations for r1, r2, c, l with constraints
                            # solution = solve([eq1, eq2, eq3, eq4], [r1, r2, l, c], dict=True)
                            # solution  = simplify(solution[0])
                            # printing.pycode(solution)
                            # solution
                            #
                            # Result solution:
                            # c = 0.5/cre
                            # l = 2.0*cre**3/(pim**2*(cim**2 + cre**2))
                            # r1 = 2.0*cre**2*(-cim*pim - cre*pre)/(pim**2*(cim**2 + cre**2))
                            # r2 = 2.0*cre**2/(cim*pim - cre*pre)
                            #
                            # Because cre is always positive (residue-flipping if real part is negative),
                            # l is always positive as all terms that could be negative appear in power of two.
                            # c is also always positive because of the residue flipping.
                            # r1 and r2 can be negative. Most simulators tolerate that. If not, put a
                            # transconductance with gm=-2/abs(r) in parallel to the resistor using the resistor's
                            # voltage as a control voltage.

                            cre=np.real(residue)
                            cim=np.imag(residue)
                            pre=np.real(pole)
                            pim=np.imag(pole)
                            c = 0.5/cre
                            l = 2.0*cre**3/(pim**2*(cim**2 + cre**2))
                            r1 = 2.0*cre**2*(-cim*pim - cre*pre)/(pim**2*(cim**2 + cre**2))
                            r2 = 2.0*cre**2/(cim*pim - cre*pre)

                            f.write(f'X{j + 1}_{i + 1}_{idx_pole} {node1} {node2} rcl_active '
                                    f'cap={c} ind={l} res1={r1} res2={r2}\n')

                    # Create the reflected wave b sources
                    f.write(f'Gb{j + 1}_{i + 1}_p {node_ref_j} s{j + 1} {node_pos_begin} {node_pos} '
                            f'{2 / np.sqrt(z0_j)}\n')

                    f.write(f'Gb{j + 1}_{i + 1}_n {node_ref_j} s{j + 1} {node_neg_begin} {node_neg} '
                            f'{2 / np.sqrt(z0_j)}\n')

                # VCCS and CCS driving the transfer impedances with incident wave a = V/(2.0*sqrt(Z0)) + I*sqrt(Z0)/2
                #
                # These current sources in parallel realize the incident wave a. The types of the sources
                # and their gains arise from the definition of the incident wave a at port i:
                # a_i=v_i/(2*sqrt(z0_i)) + i_i*sqrt(z0_i)/2
                # So we need a VCCS with a gain 1/(2*sqrt(z0_i)) in parallel with a CCCS with a gain sqrt(z0_i)/2
                f.write(f'Ga{i + 1} {node_neg} {node_pos} p{i + 1} {node_ref_i} {1 / (2 * np.sqrt(z0_i))}\n')
                f.write(f'Fa{i + 1} {node_neg} {node_pos} V{i + 1} {np.sqrt(z0_i) / 2}\n')

            f.write(f'.ENDS {fitted_model_name}\n')
            f.write('*\n')

            # Subcircuit for an RCL equivalent impedance of a complex-conjugate pole-residue pair
            f.write('.SUBCKT rcl_active 1 2 cap=1e-9 ind=100e-12 res1=1e3 res2=1e3\n')
            f.write('L1 1 3 {ind}\n')
            f.write('R1 3 2 {res1}\n')
            f.write('C1 1 2 {cap}\n')
            f.write('R2 1 2 {res2}\n')
            f.write('.ENDS rcl_active\n')

            f.write('*\n')

            # Subcircuit for an RC equivalent impedance of a real pole-residue pair
            f.write('.SUBCKT rc_passive 1 2 res=1e3 cap=1e-9\n')
            f.write('C1 1 2 {cap}\n')
            f.write('R1 1 2 {res}\n')
            f.write('.ENDS rc_passive\n')

    def _write_spice_subcircuit_s_impedance_v1b(self, file: str, fitted_model_name: str = "s_equivalent",
                                     create_reference_pins: bool = False) -> None:
        # This version has only two G sources to transfer the reflected wave b to the ports.
        # I would have guessed that it would be faster than having a G source for every pole but it is
        # indeed about 20 % slower in the linear solve. In transient it's the same speed.

        """
        Creates an equivalent N-port subcircuit based on its vector fitted S parameter responses
        in spice simulator netlist syntax (compatible with ngspice, Xyce, ...).

        Parameters
        ----------
        file : str
            Path and filename including file extension (usually .sNp) for the subcircuit file.

        fitted_model_name: str
            Name of the resulting subcircuit, default "s_equivalent"

        create_reference_pins: bool
            If set to True, the synthesized subcircuit will have N pin-pairs:
            p1 p1_ref p2 p2_ref ... pN pN_ref

            If set to False, the synthesized subcircuit will have N pins
            p1 p2 ... pN
            In this case, the reference nodes will be internally connected
            to the global ground net 0.

            The default is False

        Returns
        -------
        None

        Examples
        --------
        Load and fit the `Network`, then export the equivalent subcircuit:

        >>> nw_3port = skrf.Network('my3port.s3p')
        >>> vf = skrf.VectorFitting(nw_3port)
        >>> vf.auto_fit()
        >>> vf.write_spice_subcircuit_s('/my3port_model.sp')

        References
        ----------
        .. [1] G. Antonini, "SPICE Equivalent Circuits of Frequency-Domain Responses", IEEE Transactions on
            Electromagnetic Compatibility, vol. 45, no. 3, pp. 502-512, August 2003,
            doi: https://doi.org/10.1109/TEMC.2003.815528

        .. [2] C. -C. Chou and J. E. Schutt-Ainé, "Equivalent Circuit Synthesis of Multiport S Parameters in
            Pole–Residue Form," in IEEE Transactions on Components, Packaging and Manufacturing Technology,
            vol. 11, no. 11, pp. 1971-1979, Nov. 2021, doi: 10.1109/TCPMT.2021.3115113

        .. [3] Romano D, Antonini G, Grossner U, Kovačević-Badstübner I. Circuit synthesis techniques of
            rational models of electromagnetic systems: A tutorial paper. Int J Numer Model. 2019
            doi: https://doi.org/10.1002/jnm.2612

        """

        with open(file, 'w') as f:
            netlist_header = self._get_netlist_header(create_reference_pins=create_reference_pins,
                                                      fitted_model_name=fitted_model_name)
            f.write(netlist_header)

            # Write title line
            f.write('* EQUIVALENT CIRCUIT FOR VECTOR FITTED S-MATRIX\n')
            f.write('* Created using scikit-rf vectorFitting.py\n')
            f.write('*\n')

            # Create subcircuit pin string and reference nodes
            if create_reference_pins:
                str_input_nodes = " ".join(map(lambda x: f'p{x + 1} p{x + 1}_ref', range(self.network.nports)))
            else:
                str_input_nodes = " ".join(map(lambda x: f'p{x + 1}', range(self.network.nports)))

            f.write(f'.SUBCKT {fitted_model_name} {str_input_nodes}\n')

            for i in range(self.network.nports):
                f.write('*\n')
                f.write(f'* Port network for port {i + 1}\n')

                if create_reference_pins:
                    node_ref_i = f'p{i + 1}_ref'
                else:
                    node_ref_i = '0'

                # Reference impedance (real, i.e. resistance) of port i
                z0_i = np.real(self.network.z0[0, i])

                # Dummy voltage source (v = 0) for port current sensing (I_i)
                f.write(f'V{i + 1} p{i + 1} s{i + 1} 0\n')

                # Port reference impedance Z0_i
                f.write(f'R{i + 1} s{i + 1} {node_ref_i} {z0_i}\n')

                # Initialize node counters for a_i (p) and -a_i (n)
                n_current_pos = 0
                n_current_neg = 0
                node_pos = '0'
                node_neg = '0'

                for j in range(self.network.nports):
                    # Stacking order in VectorFitting class variables:
                    # s11, s12, s13, ..., s21, s22, s23, ...
                    i_response = j * self.network.nports + i

                    # Get idx_pole_group and idx_pole_group_member for current response
                    idx_pole_group=self.map_idx_response_to_idx_pole_group[i_response]
                    idx_pole_group_member=self.map_idx_response_to_idx_pole_group_member[i_response]

                    # Get residues
                    residues = self.residues[idx_pole_group][idx_pole_group_member]

                    # Get poles
                    poles=self.poles[idx_pole_group]

                    f.write('*\n')
                    f.write(f'* Transfer from port {i + 1} to port {j + 1}\n')

                    # Reference impedance (real, i.e. resistance) of port j
                    z0_j = np.real(self.network.z0[0, j])

                    if create_reference_pins:
                        node_ref_j = f'p{j + 1}_ref'
                    else:
                        node_ref_j = '0'

                    # Get proportional and constant term of the model
                    d = self.constant[idx_pole_group][idx_pole_group_member]
                    e = self.proportional[idx_pole_group][idx_pole_group_member]

                    # Store begin nodes of series impedance chains
                    node_pos_begin = node_pos
                    node_neg_begin = node_neg

                    # R for constant term
                    if d != 0.0:
                        # Calculated resistence can be negative, but implementation must use positive values.
                        # Append to pos or neg impedance chain depending on sign
                        if d < 0:
                            n_current_neg += 1
                            node1 = node_neg
                            node2 = node_neg = f'n_a{i + 1}_n_{n_current_neg}'

                        else:
                            n_current_pos += 1
                            node1 = node_pos
                            node2 = node_pos = f'n_a{i + 1}_p_{n_current_pos}'

                        f.write(f'R{j + 1}_{i + 1} {node1} {node2} {np.abs(d)}\n')

                    # L for proportional term
                    if e != 0.0:
                        # Append to pos or neg impedance chain depending on sign
                        if d < 0:
                            n_current_neg += 1
                            node1 = node_neg
                            node2 = node_neg = f'n_a{i + 1}_n_{n_current_neg}'
                        else:
                            n_current_pos += 1
                            node1 = node_pos
                            node2 = node_pos = f'n_a{i + 1}_p_{n_current_pos}'

                        f.write(f'L{j + 1}_{i + 1} {node1} {node2} {np.abs(e)}\n')

                    # Transfer admittances represented by poles and residues
                    for idx_pole in range(len(poles)):
                        pole = poles[idx_pole]
                        residue = residues[idx_pole]

                        # Calculated component values can be negative, but implementation must use positive values.
                        # The sign of the residue can be inverted, but then the inversion must be compensated by
                        # flipping the polarity of the VCCS control voltage
                        if np.real(residue) < 0.0:
                            # Residue multiplication with -1 required
                            residue = -1 * residue
                            n_current_neg += 1
                            node1 = node_neg
                            node2 = node_neg = f'n_a{i + 1}_n_{n_current_neg}'
                        else:
                            n_current_pos += 1
                            node1 = node_pos
                            node2 = node_pos = f'n_a{i + 1}_p_{n_current_pos}'

                        # Impedance representing S_j_i_k
                        if np.imag(pole) == 0.0:
                            # Real pole; Add parallel RC network via `rc_passive`
                            c = 1 / np.real(residue)
                            r = -1 * np.real(residue) / np.real(pole)
                            f.write(f'X{j + 1}_{i + 1}_{idx_pole} {node1} {node2} rc_passive res={r} cap={c}\n')
                        else:
                            # Complex pole of a conjugate pair; Add active or passive RCL network via `rcl_active`

                            # Calculation of the values for r1, r2, l and c using the transfer function coefficients
                            # and comparing them with the coefficients of the generic transfer function of a complex
                            # conjugated pole pair (see Antonini paper) gives the equations eq1..eq4.
                            #
                            # Transfer function of r1+sl parallel to c parallel to r2:
                            # H(s) = (s/c + r1/(lc)) / (s**2 + s(r1/l + 1/(r2 c)) + (r1/(r2 l c) + 1/(lc)))
                            #
                            # From Antonini:
                            # H'(s) = (2 cre s - 2 (cre pre + cim pim)) / (s**2 - 2 pre s + abs(p)**2)
                            #
                            # Using these abbreviations in the code:
                            # cre=np.real(residue)
                            # cim=np.imag(residue)
                            # pre=np.real(pole)
                            # pim=np.imag(pole)
                            #
                            # from sympy import symbols, Eq, solve, re, im, Abs, simplify, ask, Q, printing
                            #
                            # # Define symbols
                            # r1, r2 = symbols('r1 r2', real=True)
                            # c, l = symbols('c l', real=True, positive=True)
                            # cre = symbols('cre', real=True, positive=True)
                            # cim, pre, pim = symbols('cim pre pim', real=True)
                            #
                            # # Equations from coefficient comparison:
                            # eq1 = Eq(1 / c, 2 * cre)
                            # eq2 = Eq(r1 / (l * c), -2 * (cre * pre + cim * pim))
                            # eq3 = Eq(r1 / l + 1 / (r2 * c), -2 * pre)
                            # eq4 = Eq(r1 / (r2 * l * c) + 1 / (l * c), Abs(pre + 1j*pim)**2)
                            #
                            # # Solve system of equations for r1, r2, c, l with constraints
                            # solution = solve([eq1, eq2, eq3, eq4], [r1, r2, l, c], dict=True)
                            # solution  = simplify(solution[0])
                            # printing.pycode(solution)
                            # solution
                            #
                            # Result solution:
                            # c = 0.5/cre
                            # l = 2.0*cre**3/(pim**2*(cim**2 + cre**2))
                            # r1 = 2.0*cre**2*(-cim*pim - cre*pre)/(pim**2*(cim**2 + cre**2))
                            # r2 = 2.0*cre**2/(cim*pim - cre*pre)
                            #
                            # Because cre is always positive (residue-flipping if real part is negative),
                            # l is always positive as all terms that could be negative appear in power of two.
                            # c is also always positive because of the residue flipping.
                            # r1 and r2 can be negative. Most simulators tolerate that. If not, put a
                            # transconductance with gm=-2/abs(r) in parallel to the resistor using the resistor's
                            # voltage as a control voltage.

                            cre=np.real(residue)
                            cim=np.imag(residue)
                            pre=np.real(pole)
                            pim=np.imag(pole)
                            c = 0.5/cre
                            l = 2.0*cre**3/(pim**2*(cim**2 + cre**2))
                            r1 = 2.0*cre**2*(-cim*pim - cre*pre)/(pim**2*(cim**2 + cre**2))
                            r2 = 2.0*cre**2/(cim*pim - cre*pre)
                            if r1 < 0:
                                # Calculated r1 is negative; this gets compensated with the transconductance gt1
                                gt1 = 2 / np.abs(r1)
                            else:
                                # Transconductance gt1 not required
                                gt1 = 0.0
                            if r2 < 0:
                                # Calculated r2 is negative; this gets compensated with the transconductance gt2
                                gt2 = 2 / np.abs(r2)
                            else:
                                # Transconductance gt2 not required
                                gt2 = 0.0

                            f.write(f'X{j + 1}_{i + 1}_{idx_pole} {node1} {node2} rcl_active '
                                    f'cap={c} ind={l} res1={np.abs(r1)} res2={np.abs(r2)} gt1={gt1} gt2={gt2}\n')

                    # Create the reflected wave b sources
                    f.write(f'Gb{j + 1}_{i + 1}_p {node_ref_j} s{j + 1} {node_pos_begin} {node_pos} '
                            f'{2 / np.sqrt(z0_j)}\n')

                    f.write(f'Gb{j + 1}_{i + 1}_n {node_ref_j} s{j + 1} {node_neg_begin} {node_neg} '
                            f'{2 / np.sqrt(z0_j)}\n')

                # VCCS and CCS driving the transfer impedances with incident wave a = V/(2.0*sqrt(Z0)) + I*sqrt(Z0)/2
                #
                # These current sources in parallel realize the incident wave a. The types of the sources
                # and their gains arise from the definition of the incident wave a at port i:
                # a_i=v_i/(2*sqrt(z0_i)) + i_i*sqrt(z0_i)/2
                # So we need a VCCS with a gain 1/(2*sqrt(z0_i)) in parallel with a CCCS with a gain sqrt(z0_i)/2
                f.write(f'Ga{i + 1} {node_neg} {node_pos} p{i + 1} {node_ref_i} {1 / (2 * np.sqrt(z0_i))}\n')
                f.write(f'Fa{i + 1} {node_neg} {node_pos} V{i + 1} {np.sqrt(z0_i) / 2}\n')

            f.write(f'.ENDS {fitted_model_name}\n')
            f.write('*\n')

            # Subcircuit for an RCL equivalent impedance of a complex-conjugate pole-residue pair
            f.write('.SUBCKT rcl_active 1 2 cap=1e-9 ind=100e-12 res1=1e3 res2=1e3 gt1=2e-3 gt2=2e-3\n')
            f.write('L1 1 3 {ind}\n')
            f.write('R1 3 2 {res1}\n')
            f.write('G1 2 3 3 2 {gt1}\n')
            f.write('C1 1 2 {cap}\n')
            f.write('R2 1 2 {res2}\n')
            f.write('G2 2 1 1 2 {gt2}\n')
            f.write('.ENDS rcl_active\n')

            f.write('*\n')

            # Subcircuit for an RC equivalent impedance of a real pole-residue pair
            f.write('.SUBCKT rc_passive 1 2 res=1e3 cap=1e-9\n')
            f.write('C1 1 2 {cap}\n')
            f.write('R1 1 2 {res}\n')
            f.write('.ENDS rc_passive\n')

    def _write_spice_subcircuit_s_impedance_v2a(self, file: str, fitted_model_name: str = "s_equivalent",
                                     create_reference_pins: bool = False) -> None:
        # This version uses one G element per pole to transfer the b reflected contributions to the port network.
        # It has more components but it runs faster in the linear solve than the version 1 even if it has more
        # components. In transient the speed is the same.
        # This version writes out positive or negative R values without using G-Sources parallel to them if their
        # value is negative.

        """
        Creates an equivalent N-port subcircuit based on its vector fitted S parameter responses
        in spice simulator netlist syntax (compatible with ngspice, Xyce, ...).

        Parameters
        ----------
        file : str
            Path and filename including file extension (usually .sNp) for the subcircuit file.

        fitted_model_name: str
            Name of the resulting subcircuit, default "s_equivalent"

        create_reference_pins: bool
            If set to True, the synthesized subcircuit will have N pin-pairs:
            p1 p1_ref p2 p2_ref ... pN pN_ref

            If set to False, the synthesized subcircuit will have N pins
            p1 p2 ... pN
            In this case, the reference nodes will be internally connected
            to the global ground net 0.

            The default is False

        Returns
        -------
        None

        Examples
        --------
        Load and fit the `Network`, then export the equivalent subcircuit:

        >>> nw_3port = skrf.Network('my3port.s3p')
        >>> vf = skrf.VectorFitting(nw_3port)
        >>> vf.auto_fit()
        >>> vf.write_spice_subcircuit_s('/my3port_model.sp')

        References
        ----------
        .. [1] G. Antonini, "SPICE Equivalent Circuits of Frequency-Domain Responses", IEEE Transactions on
            Electromagnetic Compatibility, vol. 45, no. 3, pp. 502-512, August 2003,
            doi: https://doi.org/10.1109/TEMC.2003.815528

        .. [2] C. -C. Chou and J. E. Schutt-Ainé, "Equivalent Circuit Synthesis of Multiport S Parameters in
            Pole–Residue Form," in IEEE Transactions on Components, Packaging and Manufacturing Technology,
            vol. 11, no. 11, pp. 1971-1979, Nov. 2021, doi: 10.1109/TCPMT.2021.3115113

        .. [3] Romano D, Antonini G, Grossner U, Kovačević-Badstübner I. Circuit synthesis techniques of
            rational models of electromagnetic systems: A tutorial paper. Int J Numer Model. 2019
            doi: https://doi.org/10.1002/jnm.2612

        """

        with open(file, 'w') as f:
            netlist_header = self._get_netlist_header(create_reference_pins=create_reference_pins,
                                                      fitted_model_name=fitted_model_name)
            f.write(netlist_header)

            # Write title line
            f.write('* EQUIVALENT CIRCUIT FOR VECTOR FITTED S-MATRIX\n')
            f.write('* Created using scikit-rf vectorFitting.py\n')
            f.write('*\n')

            # Create subcircuit pin string and reference nodes
            if create_reference_pins:
                str_input_nodes = " ".join(map(lambda x: f'p{x + 1} p{x + 1}_ref', range(self.network.nports)))
            else:
                str_input_nodes = " ".join(map(lambda x: f'p{x + 1}', range(self.network.nports)))

            f.write(f'.SUBCKT {fitted_model_name} {str_input_nodes}\n')

            for i in range(self.network.nports):
                f.write('*\n')
                f.write(f'* Port network for port {i + 1}\n')

                if create_reference_pins:
                    node_ref_i = f'p{i + 1}_ref'
                else:
                    node_ref_i = '0'

                # Reference impedance (real, i.e. resistance) of port i
                z0_i = np.real(self.network.z0[0, i])

                # Dummy voltage source (v = 0) for port current sensing (I_i)
                f.write(f'V{i + 1} p{i + 1} s{i + 1} 0\n')

                # Port reference impedance Z0_i
                f.write(f'R{i + 1} s{i + 1} {node_ref_i} {z0_i}\n')

                # Initialize node counters for a_i (p) and -a_i (n)
                n_current = 0
                node = '0'

                for j in range(self.network.nports):
                    # Stacking order in VectorFitting class variables:
                    # s11, s12, s13, ..., s21, s22, s23, ...
                    i_response = j * self.network.nports + i

                    # Get idx_pole_group and idx_pole_group_member for current response
                    idx_pole_group=self.map_idx_response_to_idx_pole_group[i_response]
                    idx_pole_group_member=self.map_idx_response_to_idx_pole_group_member[i_response]

                    # Get residues
                    residues = self.residues[idx_pole_group][idx_pole_group_member]

                    # Get poles
                    poles=self.poles[idx_pole_group]

                    f.write('*\n')
                    f.write(f'* Transfer from port {i + 1} to port {j + 1}\n')

                    # Reference impedance (real, i.e. resistance) of port j
                    z0_j = np.real(self.network.z0[0, j])

                    if create_reference_pins:
                        node_ref_j = f'p{j + 1}_ref'
                    else:
                        node_ref_j = '0'

                    # Get proportional and constant term of the model
                    d = self.constant[idx_pole_group][idx_pole_group_member]
                    e = self.proportional[idx_pole_group][idx_pole_group_member]

                    # R for constant term
                    if d != 0.0:
                        # Increment node counter
                        n_current += 1
                        node1 = node
                        node2 = node = f'n_a{i + 1}_{n_current}'
                        f.write(f'R{j + 1}_{i + 1} {node1} {node2} {np.abs(d)}\n')

                        # Calculated resistence can be negative, but implementation must use positive values.
                        if d < 0:
                            f.write(f'Gb{j + 1}_{i + 1}_d {node_ref_j} s{j + 1} {node2} {node1} '
                                f'{2 / np.sqrt(z0_j)}\n')
                        else:
                            f.write(f'Gb{j + 1}_{i + 1}_d {node_ref_j} s{j + 1} {node1} {node2} '
                                f'{2 / np.sqrt(z0_j)}\n')

                    # L for proportional term
                    if e != 0.0:
                        # Increment node counter
                        n_current += 1
                        node1 = node
                        node2 = node = f'n_a{i + 1}_{n_current}'
                        f.write(f'L{j + 1}_{i + 1} {node1} {node2} {np.abs(e)}\n')

                        # Calculated resistence can be negative, but implementation must use positive values.
                        if d < 0:
                            f.write(f'Gb{j + 1}_{i + 1}_e {node_ref_j} s{j + 1} {node2} {node1} '
                                f'{2 / np.sqrt(z0_j)}\n')
                        else:
                            f.write(f'Gb{j + 1}_{i + 1}_e {node_ref_j} s{j + 1} {node1} {node2} '
                                f'{2 / np.sqrt(z0_j)}\n')

                    # Transfer admittances represented by poles and residues
                    for idx_pole in range(len(poles)):
                        pole = poles[idx_pole]
                        residue = residues[idx_pole]

                        # Increment node counter
                        n_current += 1
                        node1 = node
                        node2 = node = f'n_a{i + 1}_{n_current}'

                        # Calculated component values can be negative, but implementation must use positive values.
                        # The sign of the residue can be inverted, but then the inversion must be compensated by
                        # flipping the polarity of the VCCS control voltage
                        if np.real(residue) < 0.0:
                            # Residue multiplication with -1 required
                            residue = -1 * residue
                            f.write(f'Gb{j + 1}_{i + 1}_{idx_pole} {node_ref_j} s{j + 1} {node2} {node1} '
                                f'{2 / np.sqrt(z0_j)}\n')
                        else:
                            f.write(f'Gb{j + 1}_{i + 1}_{idx_pole} {node_ref_j} s{j + 1} {node1} {node2} '
                                f'{2 / np.sqrt(z0_j)}\n')

                        # Impedance representing S_j_i_k
                        if np.imag(pole) == 0.0:
                            # Real pole; Add parallel RC network via `rc_passive`
                            c = 1 / np.real(residue)
                            r = -1 * np.real(residue) / np.real(pole)
                            f.write(f'X{j + 1}_{i + 1}_{idx_pole} {node1} {node2} rc_passive res={r} cap={c}\n')

                        else:
                            # Complex pole of a conjugate pair; Add active or passive RCL network via `rcl_active`

                            # Calculation of the values for r1, r2, l and c using the transfer function coefficients
                            # and comparing them with the coefficients of the generic transfer function of a complex
                            # conjugated pole pair (see Antonini paper) gives the equations eq1..eq4.
                            #
                            # Transfer function of r1+sl parallel to c parallel to r2:
                            # H(s) = (s/c + r1/(lc)) / (s**2 + s(r1/l + 1/(r2 c)) + (r1/(r2 l c) + 1/(lc)))
                            #
                            # From Antonini:
                            # H'(s) = (2 cre s - 2 (cre pre + cim pim)) / (s**2 - 2 pre s + abs(p)**2)
                            #
                            # Using these abbreviations in the code:
                            # cre=np.real(residue)
                            # cim=np.imag(residue)
                            # pre=np.real(pole)
                            # pim=np.imag(pole)
                            #
                            # from sympy import symbols, Eq, solve, re, im, Abs, simplify, ask, Q, printing
                            #
                            # # Define symbols
                            # r1, r2 = symbols('r1 r2', real=True)
                            # c, l = symbols('c l', real=True, positive=True)
                            # cre = symbols('cre', real=True, positive=True)
                            # cim, pre, pim = symbols('cim pre pim', real=True)
                            #
                            # # Equations from coefficient comparison:
                            # eq1 = Eq(1 / c, 2 * cre)
                            # eq2 = Eq(r1 / (l * c), -2 * (cre * pre + cim * pim))
                            # eq3 = Eq(r1 / l + 1 / (r2 * c), -2 * pre)
                            # eq4 = Eq(r1 / (r2 * l * c) + 1 / (l * c), Abs(pre + 1j*pim)**2)
                            #
                            # # Solve system of equations for r1, r2, c, l with constraints
                            # solution = solve([eq1, eq2, eq3, eq4], [r1, r2, l, c], dict=True)
                            # solution  = simplify(solution[0])
                            # printing.pycode(solution)
                            # solution
                            #
                            # Result solution:
                            # c = 0.5/cre
                            # l = 2.0*cre**3/(pim**2*(cim**2 + cre**2))
                            # r1 = 2.0*cre**2*(-cim*pim - cre*pre)/(pim**2*(cim**2 + cre**2))
                            # r2 = 2.0*cre**2/(cim*pim - cre*pre)
                            #
                            # Because cre is always positive (residue-flipping if real part is negative),
                            # l is always positive as all terms that could be negative appear in power of two.
                            # c is also always positive because of the residue flipping.
                            # r1 and r2 can be negative. Most simulators tolerate that. If not, put a
                            # transconductance with gm=-2/abs(r) in parallel to the resistor using the resistor's
                            # voltage as a control voltage.

                            cre=np.real(residue)
                            cim=np.imag(residue)
                            pre=np.real(pole)
                            pim=np.imag(pole)
                            c = 0.5/cre
                            l = 2.0*cre**3/(pim**2*(cim**2 + cre**2))
                            r1 = 2.0*cre**2*(-cim*pim - cre*pre)/(pim**2*(cim**2 + cre**2))
                            r2 = 2.0*cre**2/(cim*pim - cre*pre)

                            f.write(f'X{j + 1}_{i + 1}_{idx_pole} {node1} {node2} rcl_active '
                                    f'cap={c} ind={l} res1={r1} res2={r2}\n')

                # VCCS and CCS driving the transfer impedances with incident wave a = V/(2.0*sqrt(Z0)) + I*sqrt(Z0)/2
                #
                # These current sources in parallel realize the incident wave a. The types of the sources
                # and their gains arise from the definition of the incident wave a at port i:
                # a_i=v_i/(2*sqrt(z0_i)) + i_i*sqrt(z0_i)/2
                # So we need a VCCS with a gain 1/(2*sqrt(z0_i)) in parallel with a CCCS with a gain sqrt(z0_i)/2
                f.write(f'Ga{i + 1} 0 {node} p{i + 1} {node_ref_i} {1 / (2 * np.sqrt(z0_i))}\n')
                f.write(f'Fa{i + 1} 0 {node} V{i + 1} {np.sqrt(z0_i) / 2}\n')

            f.write(f'.ENDS {fitted_model_name}\n')
            f.write('*\n')

            # Subcircuit for an RCL equivalent impedance of a complex-conjugate pole-residue pair
            f.write('.SUBCKT rcl_active 1 2 cap=1e-9 ind=100e-12 res1=1e3 res2=1e3\n')
            f.write('L1 1 3 {ind}\n')
            f.write('R1 3 2 {res1}\n')
            f.write('C1 1 2 {cap}\n')
            f.write('R2 1 2 {res2}\n')
            f.write('.ENDS rcl_active\n')

            f.write('*\n')

            # Subcircuit for an RC equivalent impedance of a real pole-residue pair
            f.write('.SUBCKT rc_passive 1 2 res=1e3 cap=1e-9\n')
            f.write('C1 1 2 {cap}\n')
            f.write('R1 1 2 {res}\n')
            f.write('.ENDS rc_passive\n')

    def _write_spice_subcircuit_s_impedance_v2b(self, file: str, fitted_model_name: str = "s_equivalent",
                                     create_reference_pins: bool = False) -> None:
        # This version uses one G element per pole to transfer the b reflected contributions to the port network.
        # It has more components but it runs faster in the linear solve than the version 1 even if it has more
        # components. In transient the speed is the same.
        # This version writes out only positive R values and adds G-Sources parallel to them if their value is
        # negative.

        """
        Creates an equivalent N-port subcircuit based on its vector fitted S parameter responses
        in spice simulator netlist syntax (compatible with ngspice, Xyce, ...).

        Parameters
        ----------
        file : str
            Path and filename including file extension (usually .sNp) for the subcircuit file.

        fitted_model_name: str
            Name of the resulting subcircuit, default "s_equivalent"

        create_reference_pins: bool
            If set to True, the synthesized subcircuit will have N pin-pairs:
            p1 p1_ref p2 p2_ref ... pN pN_ref

            If set to False, the synthesized subcircuit will have N pins
            p1 p2 ... pN
            In this case, the reference nodes will be internally connected
            to the global ground net 0.

            The default is False

        Returns
        -------
        None

        Examples
        --------
        Load and fit the `Network`, then export the equivalent subcircuit:

        >>> nw_3port = skrf.Network('my3port.s3p')
        >>> vf = skrf.VectorFitting(nw_3port)
        >>> vf.auto_fit()
        >>> vf.write_spice_subcircuit_s('/my3port_model.sp')

        References
        ----------
        .. [1] G. Antonini, "SPICE Equivalent Circuits of Frequency-Domain Responses", IEEE Transactions on
            Electromagnetic Compatibility, vol. 45, no. 3, pp. 502-512, August 2003,
            doi: https://doi.org/10.1109/TEMC.2003.815528

        .. [2] C. -C. Chou and J. E. Schutt-Ainé, "Equivalent Circuit Synthesis of Multiport S Parameters in
            Pole–Residue Form," in IEEE Transactions on Components, Packaging and Manufacturing Technology,
            vol. 11, no. 11, pp. 1971-1979, Nov. 2021, doi: 10.1109/TCPMT.2021.3115113

        .. [3] Romano D, Antonini G, Grossner U, Kovačević-Badstübner I. Circuit synthesis techniques of
            rational models of electromagnetic systems: A tutorial paper. Int J Numer Model. 2019
            doi: https://doi.org/10.1002/jnm.2612

        """

        with open(file, 'w') as f:
            netlist_header = self._get_netlist_header(create_reference_pins=create_reference_pins,
                                                      fitted_model_name=fitted_model_name)
            f.write(netlist_header)

            # Write title line
            f.write('* EQUIVALENT CIRCUIT FOR VECTOR FITTED S-MATRIX\n')
            f.write('* Created using scikit-rf vectorFitting.py\n')
            f.write('*\n')

            # Create subcircuit pin string and reference nodes
            if create_reference_pins:
                str_input_nodes = " ".join(map(lambda x: f'p{x + 1} p{x + 1}_ref', range(self.network.nports)))
            else:
                str_input_nodes = " ".join(map(lambda x: f'p{x + 1}', range(self.network.nports)))

            f.write(f'.SUBCKT {fitted_model_name} {str_input_nodes}\n')

            for i in range(self.network.nports):
                f.write('*\n')
                f.write(f'* Port network for port {i + 1}\n')

                if create_reference_pins:
                    node_ref_i = f'p{i + 1}_ref'
                else:
                    node_ref_i = '0'

                # Reference impedance (real, i.e. resistance) of port i
                z0_i = np.real(self.network.z0[0, i])

                # Dummy voltage source (v = 0) for port current sensing (I_i)
                f.write(f'V{i + 1} p{i + 1} s{i + 1} 0\n')

                # Port reference impedance Z0_i
                f.write(f'R{i + 1} s{i + 1} {node_ref_i} {z0_i}\n')

                # Initialize node counters for a_i (p) and -a_i (n)
                n_current = 0
                node = '0'

                for j in range(self.network.nports):
                    # Stacking order in VectorFitting class variables:
                    # s11, s12, s13, ..., s21, s22, s23, ...
                    i_response = j * self.network.nports + i

                    # Get idx_pole_group and idx_pole_group_member for current response
                    idx_pole_group=self.map_idx_response_to_idx_pole_group[i_response]
                    idx_pole_group_member=self.map_idx_response_to_idx_pole_group_member[i_response]

                    # Get residues
                    residues = self.residues[idx_pole_group][idx_pole_group_member]

                    # Get poles
                    poles=self.poles[idx_pole_group]

                    f.write('*\n')
                    f.write(f'* Transfer from port {i + 1} to port {j + 1}\n')

                    # Reference impedance (real, i.e. resistance) of port j
                    z0_j = np.real(self.network.z0[0, j])

                    if create_reference_pins:
                        node_ref_j = f'p{j + 1}_ref'
                    else:
                        node_ref_j = '0'

                    # Get proportional and constant term of the model
                    d = self.constant[idx_pole_group][idx_pole_group_member]
                    e = self.proportional[idx_pole_group][idx_pole_group_member]

                    # R for constant term
                    if d != 0.0:
                        # Increment node counter
                        n_current += 1
                        node1 = node
                        node2 = node = f'n_a{i + 1}_{n_current}'
                        f.write(f'R{j + 1}_{i + 1} {node1} {node2} {np.abs(d)}\n')

                        # Calculated resistence can be negative, but implementation must use positive values.
                        if d < 0:
                            f.write(f'Gb{j + 1}_{i + 1}_d {node_ref_j} s{j + 1} {node2} {node1} '
                                f'{2 / np.sqrt(z0_j)}\n')
                        else:
                            f.write(f'Gb{j + 1}_{i + 1}_d {node_ref_j} s{j + 1} {node1} {node2} '
                                f'{2 / np.sqrt(z0_j)}\n')

                    # L for proportional term
                    if e != 0.0:
                        # Increment node counter
                        n_current += 1
                        node1 = node
                        node2 = node = f'n_a{i + 1}_{n_current}'
                        f.write(f'L{j + 1}_{i + 1} {node1} {node2} {np.abs(e)}\n')

                        # Calculated resistence can be negative, but implementation must use positive values.
                        if d < 0:
                            f.write(f'Gb{j + 1}_{i + 1}_e {node_ref_j} s{j + 1} {node2} {node1} '
                                f'{2 / np.sqrt(z0_j)}\n')
                        else:
                            f.write(f'Gb{j + 1}_{i + 1}_e {node_ref_j} s{j + 1} {node1} {node2} '
                                f'{2 / np.sqrt(z0_j)}\n')

                    # Transfer admittances represented by poles and residues
                    for idx_pole in range(len(poles)):
                        pole = poles[idx_pole]
                        residue = residues[idx_pole]

                        # Increment node counter
                        n_current += 1
                        node1 = node
                        node2 = node = f'n_a{i + 1}_{n_current}'

                        # Calculated component values can be negative, but implementation must use positive values.
                        # The sign of the residue can be inverted, but then the inversion must be compensated by
                        # flipping the polarity of the VCCS control voltage
                        if np.real(residue) < 0.0:
                            # Residue multiplication with -1 required
                            residue = -1 * residue
                            f.write(f'Gb{j + 1}_{i + 1}_{idx_pole} {node_ref_j} s{j + 1} {node2} {node1} '
                                f'{2 / np.sqrt(z0_j)}\n')
                        else:
                            f.write(f'Gb{j + 1}_{i + 1}_{idx_pole} {node_ref_j} s{j + 1} {node1} {node2} '
                                f'{2 / np.sqrt(z0_j)}\n')

                        # Impedance representing S_j_i_k
                        if np.imag(pole) == 0.0:
                            # Real pole; Add parallel RC network via `rc_passive`
                            c = 1 / np.real(residue)
                            r = -1 * np.real(residue) / np.real(pole)
                            f.write(f'X{j + 1}_{i + 1}_{idx_pole} {node1} {node2} rc_passive res={r} cap={c}\n')

                        else:
                            # Complex pole of a conjugate pair; Add active or passive RCL network via `rcl_active`

                            # Calculation of the values for r1, r2, l and c using the transfer function coefficients
                            # and comparing them with the coefficients of the generic transfer function of a complex
                            # conjugated pole pair (see Antonini paper) gives the equations eq1..eq4.
                            #
                            # Transfer function of r1+sl parallel to c parallel to r2:
                            # H(s) = (s/c + r1/(lc)) / (s**2 + s(r1/l + 1/(r2 c)) + (r1/(r2 l c) + 1/(lc)))
                            #
                            # From Antonini:
                            # H'(s) = (2 cre s - 2 (cre pre + cim pim)) / (s**2 - 2 pre s + abs(p)**2)
                            #
                            # Using these abbreviations in the code:
                            # cre=np.real(residue)
                            # cim=np.imag(residue)
                            # pre=np.real(pole)
                            # pim=np.imag(pole)
                            #
                            # from sympy import symbols, Eq, solve, re, im, Abs, simplify, ask, Q, printing
                            #
                            # # Define symbols
                            # r1, r2 = symbols('r1 r2', real=True)
                            # c, l = symbols('c l', real=True, positive=True)
                            # cre = symbols('cre', real=True, positive=True)
                            # cim, pre, pim = symbols('cim pre pim', real=True)
                            #
                            # # Equations from coefficient comparison:
                            # eq1 = Eq(1 / c, 2 * cre)
                            # eq2 = Eq(r1 / (l * c), -2 * (cre * pre + cim * pim))
                            # eq3 = Eq(r1 / l + 1 / (r2 * c), -2 * pre)
                            # eq4 = Eq(r1 / (r2 * l * c) + 1 / (l * c), Abs(pre + 1j*pim)**2)
                            #
                            # # Solve system of equations for r1, r2, c, l with constraints
                            # solution = solve([eq1, eq2, eq3, eq4], [r1, r2, l, c], dict=True)
                            # solution  = simplify(solution[0])
                            # printing.pycode(solution)
                            # solution
                            #
                            # Result solution:
                            # c = 0.5/cre
                            # l = 2.0*cre**3/(pim**2*(cim**2 + cre**2))
                            # r1 = 2.0*cre**2*(-cim*pim - cre*pre)/(pim**2*(cim**2 + cre**2))
                            # r2 = 2.0*cre**2/(cim*pim - cre*pre)
                            #
                            # Because cre is always positive (residue-flipping if real part is negative),
                            # l is always positive as all terms that could be negative appear in power of two.
                            # c is also always positive because of the residue flipping.
                            # r1 and r2 can be negative. Most simulators tolerate that. If not, put a
                            # transconductance with gm=-2/abs(r) in parallel to the resistor using the resistor's
                            # voltage as a control voltage.

                            cre=np.real(residue)
                            cim=np.imag(residue)
                            pre=np.real(pole)
                            pim=np.imag(pole)
                            c = 0.5/cre
                            l = 2.0*cre**3/(pim**2*(cim**2 + cre**2))
                            r1 = 2.0*cre**2*(-cim*pim - cre*pre)/(pim**2*(cim**2 + cre**2))
                            r2 = 2.0*cre**2/(cim*pim - cre*pre)

                            if r1 < 0:
                                # Calculated r1 is negative; this gets compensated with the transconductance gt1
                                gt1 = 2 / np.abs(r1)
                            else:
                                # Transconductance gt1 not required
                                gt1 = 0.0
                            if r2 < 0:
                                # Calculated r2 is negative; this gets compensated with the transconductance gt2
                                gt2 = 2 / np.abs(r2)
                            else:
                                # Transconductance gt2 not required
                                gt2 = 0.0

                            f.write(f'X{j + 1}_{i + 1}_{idx_pole} {node1} {node2} rcl_active '
                                    f'cap={c} ind={l} res1={np.abs(r1)} res2={np.abs(r2)} gt1={gt1} gt2={gt2}\n')

                # VCCS and CCS driving the transfer impedances with incident wave a = V/(2.0*sqrt(Z0)) + I*sqrt(Z0)/2
                #
                # These current sources in parallel realize the incident wave a. The types of the sources
                # and their gains arise from the definition of the incident wave a at port i:
                # a_i=v_i/(2*sqrt(z0_i)) + i_i*sqrt(z0_i)/2
                # So we need a VCCS with a gain 1/(2*sqrt(z0_i)) in parallel with a CCCS with a gain sqrt(z0_i)/2
                f.write(f'Ga{i + 1} 0 {node} p{i + 1} {node_ref_i} {1 / (2 * np.sqrt(z0_i))}\n')
                f.write(f'Fa{i + 1} 0 {node} V{i + 1} {np.sqrt(z0_i) / 2}\n')

            f.write(f'.ENDS {fitted_model_name}\n')
            f.write('*\n')

            # Subcircuit for an RCL equivalent impedance of a complex-conjugate pole-residue pair
            f.write('.SUBCKT rcl_active 1 2 cap=1e-9 ind=100e-12 res1=1e3 res2=1e3 gt1=2e-3 gt2=2e-3\n')
            f.write('L1 1 3 {ind}\n')
            f.write('R1 3 2 {res1}\n')
            f.write('G1 2 3 3 2 {gt1}\n')
            f.write('C1 1 2 {cap}\n')
            f.write('R2 1 2 {res2}\n')
            f.write('G2 2 1 1 2 {gt2}\n')
            f.write('.ENDS rcl_active\n')

            f.write('*\n')

            # Subcircuit for an RC equivalent impedance of a real pole-residue pair
            f.write('.SUBCKT rc_passive 1 2 res=1e3 cap=1e-9\n')
            f.write('C1 1 2 {cap}\n')
            f.write('R1 1 2 {res}\n')
            f.write('.ENDS rc_passive\n')

    def _write_spice_subcircuit_s_admittance_v1(self, file: str, fitted_model_name: str = "s_equivalent",
                                     create_reference_pins: bool=False) -> None:
        # This version uses only two controlled sources to transfer the b reflected wave to the port networks
        # but it runs extremely slow in Xyce compared to the impedance based version.
        """
        Creates an equivalent N-port subcircuit based on its vector fitted S parameter responses
        in spice simulator netlist syntax

        Parameters
        ----------
        file : str
            Path and filename including file extension (usually .sNp) for the subcircuit file.

        fitted_model_name: str
            Name of the resulting model, default "s_equivalent"

        create_reference_pins: bool
            If set to True, the synthesized subcircuit will have N pin-pairs:
            P0, P0_reference, ..., PN, PN_reference

            If set to False, the synthesized subcircuit will have N pins
            P0, ..., PN
            In this case, the reference nodes will be internally connected
            to the global ground net 0.

            The default is False

        Returns
        -------
        None

        Examples
        --------
        Load and fit the `Network`, then export the equivalent subcircuit:

        >>> nw_3port = skrf.Network('my3port.s3p')
        >>> vf = skrf.VectorFitting(nw_3port)
        >>> vf.vector_fit(n_poles_real=1, n_poles_cmplx=4)
        >>> vf.write_spice_subcircuit_s('/my3port_model.sp')

        References
        ----------
        .. [1] G. Antonini, "SPICE Equivalent Circuits of Frequency-Domain Responses", IEEE Transactions on
            Electromagnetic Compatibility, vol. 45, no. 3, pp. 502-512, August 2003,
            doi: https://doi.org/10.1109/TEMC.2003.815528

        .. [2] C. -C. Chou and J. E. Schutt-Ainé, "Equivalent Circuit Synthesis of Multiport S Parameters in
            Pole–Residue Form," in IEEE Transactions on Components, Packaging and Manufacturing Technology,
            vol. 11, no. 11, pp. 1971-1979, Nov. 2021, doi: 10.1109/TCPMT.2021.3115113

        .. [3] Romano D, Antonini G, Grossner U, Kovačević-Badstübner I. Circuit synthesis techniques of
            rational models of electromagnetic systems: A tutorial paper. Int J Numer Model. 2019
            doi: https://doi.org/10.1002/jnm.2612

        """

        # List of subcircuits for the equivalent admittances
        subcircuits = []

        # Provides a unique subcircuit identifier (X1, X2, X3, ...)
        def get_new_subckt_identifier():
            subcircuits.append(f'X{len(subcircuits) + 1}')
            return subcircuits[-1]

        with open(file, 'w') as f:
            netlist_header = self._get_netlist_header(create_reference_pins=create_reference_pins,
                                                      fitted_model_name=fitted_model_name)
            f.write(netlist_header)

            # Write title line
            f.write('* EQUIVALENT CIRCUIT FOR VECTOR FITTED S-MATRIX\n')
            f.write('* Created using scikit-rf vectorFitting.py\n\n')

            # Create subcircuit pin string and reference nodes
            if create_reference_pins:
                str_input_nodes = " ".join(map(lambda x: f'p{x + 1} r{x + 1}', range(self.network.nports)))
                ref_nodes = list(map(lambda x: f'r{x + 1}', range(self.network.nports)))
            else:
                str_input_nodes = " ".join(map(lambda x: f'p{x + 1}', range(self.network.nports)))
                ref_nodes = list(map(lambda x: '0', range(self.network.nports)))

            f.write(f'.SUBCKT {fitted_model_name} {str_input_nodes}\n')

            for n in range(self.network.nports):
                f.write(f'\n* Port network for port {n + 1}\n')

                # Calculate sqrt of Z0 for port current port
                sqrt_Z0_n=np.sqrt(np.real(self.network.z0[0, n]))

                # Port reference impedance Z0
                f.write(f'R_ref_{n + 1} p{n+1} a{n + 1} {np.real(self.network.z0[0, n])}\n')

                # CCVS implementing the reflected wave b.
                # Also used as current sensor to measure the input current
                #
                # The type of the source (voltage source) and its gain 2*sqrt(Z0N) arise from the
                # definition of the reflected wave b at port N: bN=(VN-Z0N*IN)/(2*sqrt(Z0N))
                # This equation represents the Kirchhoff voltage law of the port network:
                # 2*sqrt(Z0N)*bN=VN-Z0N*IN
                # The left hand side of the equation is realized with a (controlled) voltage
                # source with a gain of 2*sqrt(Z0N).
                f.write(f'H_b_{n + 1} a{n + 1} {ref_nodes[n]} V_c_{n + 1} {2.0*sqrt_Z0_n}\n')

                f.write(f'* Differential incident wave a sources for transfer from port {n + 1}\n')

                # CCVS and VCVS driving the transfer admittances with incident wave a = V/(2.0*sqrt(Z0)) + I*sqrt(Z0)/2
                #
                # These voltage sources in series realize the incident wave a. The types of the sources
                # and their gains arise from the definition of the incident wave a at port N:
                # aN=VN/(2*sqrt(Z0N)) + IN*sqrt(Z0N)/2
                # So we need a VCVS with a gain 1/(2*sqrt(Z0N)) in series with a CCVS with a gain sqrt(Z0N)/2
                f.write(f'H_p_{n + 1} nt_p_{n + 1} nts_p_{n + 1} H_b_{n + 1} {0.5*sqrt_Z0_n}\n')
                f.write(f'E_p_{n + 1} nts_p_{n + 1} {ref_nodes[n]} p{n + 1} {ref_nodes[n]} {1.0/(2.0*sqrt_Z0_n)}\n')

                # VCVS driving the transfer admittances with -a
                #
                # This source just copies the a wave and multiplies it by -1 to implement the negative side
                # of the differential a wave. The inversion of the sign is done by the connecting the source
                # in opposite direction to the reference node. Thus, the gain is 1.
                f.write(f'E_n_{n + 1} {ref_nodes[n]} nt_n_{n + 1} nt_p_{n + 1} {ref_nodes[n]} 1\n')

                f.write(f'* Current sensor on center node for transfer to port {n + 1}\n')

                # Current sensor for the transfer to current port
                f.write(f'V_c_{n + 1} nt_c_{n + 1} {ref_nodes[n]} 0\n')

                for j in range(self.network.nports):
                    f.write(f'* Transfer network from port {j + 1} to port {n + 1}\n')

                    # Stacking order in VectorFitting class variables:
                    # s11, s12, s13, ..., s21, s22, s23, ...
                    i_response = n * self.network.nports + j

                    # Get idx_pole_group and idx_pole_group_member for current response
                    idx_pole_group=self.map_idx_response_to_idx_pole_group[i_response]
                    idx_pole_group_member=self.map_idx_response_to_idx_pole_group_member[i_response]

                    # Start with proportional and constant term of the model
                    # H(s) = d + s * e  model
                    # Y(s) = G + s * C  equivalent admittance
                    g = self.constant[idx_pole_group][idx_pole_group_member]
                    c = self.proportional[idx_pole_group][idx_pole_group_member]

                    # R for constant term
                    if g < 0:
                        f.write(f'R{n + 1}_{j + 1} nt_n_{j + 1} nt_c_{n + 1} {np.abs(1 / g)}\n')
                    elif g > 0:
                        f.write(f'R{n + 1}_{j + 1} nt_p_{j + 1} nt_c_{n + 1} {1 / g}\n')

                    # C for proportional term
                    if c < 0:
                        f.write(f'C{n + 1}_{j + 1} nt_n_{j + 1} nt_c_{n + 1} {np.abs(c)}\n')
                    elif c > 0:
                        f.write(f'C{n + 1}_{j + 1} nt_p_{j + 1} nt_c_{n + 1} {c}\n')

                    # Get residues
                    residues = self.residues[idx_pole_group][idx_pole_group_member]

                    # Get poles
                    poles=self.poles[idx_pole_group]

                    # Transfer admittances represented by poles and residues
                    for idx_pole in range(len(poles)):
                        pole = poles[idx_pole]
                        residue = residues[idx_pole]
                        node = get_new_subckt_identifier()

                        if np.real(residue) < 0.0:
                            # Multiplication with -1 required, otherwise the values for RLC would be negative.
                            # This gets compensated by inverting the transfer current direction for this subcircuit
                            residue = -1 * residue
                            node += f' nt_n_{j + 1} nt_c_{n + 1}'
                        else:
                            node += f' nt_p_{j + 1} nt_c_{n + 1}'

                        if np.imag(pole) == 0.0:
                            # Real pole; Add rl_admittance
                            l = 1 / np.real(residue)
                            r = -1 * np.real(pole) / np.real(residue)
                            f.write(node + f' rl_admittance res={r} ind={l}\n')
                        else:
                            # Complex pole of a conjugate pair; Add rcl_vccs_admittance
                            r = -1 * np.real(pole) / np.real(residue)
                            c = 2 * np.real(residue) / (np.abs(pole) ** 2)
                            l = 1 / (2 * np.real(residue))
                            b = -2 * (np.real(residue) * np.real(pole) + np.imag(residue) * np.imag(pole))
                            gm = b * l * c
                            f.write(node + f' rcl_vccs_admittance res={r} cap={c} ind={l} gm={gm}\n')

            f.write(f'.ENDS {fitted_model_name}\n\n')

            # Subcircuit for an RLCG equivalent admittance of a complex-conjugate pole-residue pair
            f.write('.SUBCKT rcl_vccs_admittance n_pos n_neg res=1e3 cap=1e-9 ind=100e-12 gm=1e-3\n')
            f.write('L1 n_pos 1 {ind}\n')
            f.write('C1 1 2 {cap}\n')
            f.write('R1 2 n_neg {res}\n')
            f.write('G1 n_pos n_neg 1 2 {gm}\n')
            f.write('.ENDS rcl_vccs_admittance\n\n')

            # Subcircuit for an RL equivalent admittance of a real pole-residue pair
            f.write('.SUBCKT rl_admittance n_pos n_neg res=1e3 ind=100e-12\n')
            f.write('L1 n_pos 1 {ind}\n')
            f.write('R1 1 n_neg {res}\n')
            f.write('.ENDS rl_admittance\n\n')

    def _write_spice_subcircuit_s_admittance_v2(self, file: str, fitted_model_name: str = "s_equivalent",
                                     create_reference_pins: bool=False) -> None:
        # This version also uses only two controlled sources for the transfer of the b reflected wave to the
        # port networks but it uses a parallel port network instead of a serial one. This uses only half the
        # simulation time as the v1 version just by replacing n_ports * CCVS with n_ports * CCCS but it is
        # still way slower than the impedance versions.

        """
        Creates an equivalent N-port subcircuit based on its vector fitted S parameter responses
        in spice simulator netlist syntax

        Parameters
        ----------
        file : str
            Path and filename including file extension (usually .sNp) for the subcircuit file.

        fitted_model_name: str
            Name of the resulting model, default "s_equivalent"

        create_reference_pins: bool
            If set to True, the synthesized subcircuit will have N pin-pairs:
            P0, P0_reference, ..., PN, PN_reference

            If set to False, the synthesized subcircuit will have N pins
            P0, ..., PN
            In this case, the reference nodes will be internally connected
            to the global ground net 0.

            The default is False

        Returns
        -------
        None

        Examples
        --------
        Load and fit the `Network`, then export the equivalent subcircuit:

        >>> nw_3port = skrf.Network('my3port.s3p')
        >>> vf = skrf.VectorFitting(nw_3port)
        >>> vf.vector_fit(n_poles_real=1, n_poles_cmplx=4)
        >>> vf.write_spice_subcircuit_s('/my3port_model.sp')

        References
        ----------
        .. [1] G. Antonini, "SPICE Equivalent Circuits of Frequency-Domain Responses", IEEE Transactions on
            Electromagnetic Compatibility, vol. 45, no. 3, pp. 502-512, August 2003,
            doi: https://doi.org/10.1109/TEMC.2003.815528

        .. [2] C. -C. Chou and J. E. Schutt-Ainé, "Equivalent Circuit Synthesis of Multiport S Parameters in
            Pole–Residue Form," in IEEE Transactions on Components, Packaging and Manufacturing Technology,
            vol. 11, no. 11, pp. 1971-1979, Nov. 2021, doi: 10.1109/TCPMT.2021.3115113

        .. [3] Romano D, Antonini G, Grossner U, Kovačević-Badstübner I. Circuit synthesis techniques of
            rational models of electromagnetic systems: A tutorial paper. Int J Numer Model. 2019
            doi: https://doi.org/10.1002/jnm.2612

        """

        # List of subcircuits for the equivalent admittances
        subcircuits = []

        # Provides a unique subcircuit identifier (X1, X2, X3, ...)
        def get_new_subckt_identifier():
            subcircuits.append(f'X{len(subcircuits) + 1}')
            return subcircuits[-1]

        with open(file, 'w') as f:
            netlist_header = self._get_netlist_header(create_reference_pins=create_reference_pins,
                                                      fitted_model_name=fitted_model_name)
            f.write(netlist_header)

            # Write title line
            f.write('* EQUIVALENT CIRCUIT FOR VECTOR FITTED S-MATRIX\n')
            f.write('* Created using scikit-rf vectorFitting.py\n\n')

            # Create subcircuit pin string and reference nodes
            if create_reference_pins:
                str_input_nodes = " ".join(map(lambda x: f'p{x + 1} r{x + 1}', range(self.network.nports)))
                ref_nodes = list(map(lambda x: f'r{x + 1}', range(self.network.nports)))
            else:
                str_input_nodes = " ".join(map(lambda x: f'p{x + 1}', range(self.network.nports)))
                ref_nodes = list(map(lambda x: '0', range(self.network.nports)))

            f.write(f'.SUBCKT {fitted_model_name} {str_input_nodes}\n')

            for n in range(self.network.nports):
                f.write(f'\n* Port network for port {n + 1}\n')

                # Calculate sqrt of Z0 for port current port
                sqrt_Z0_n=np.sqrt(np.real(self.network.z0[0, n]))

                # Input current sensor
                f.write(f'V_p_{n + 1} p{n + 1} a{n + 1} 0\n')

                # Port reference impedance Z0
                f.write(f'R_ref_{n + 1} a{n + 1} {ref_nodes[n]} {np.real(self.network.z0[0, n])}\n')

                # CCCS implementing the reflected wave b.
                #
                # The type of the source (current source) and its gain 2/sqrt(Z0N) arise from the
                # definition of the reflected wave b at port N: bN=(VN-Z0N*IN)/(2*sqrt(Z0N))
                # This equation represents the Kirchhoff voltage law of the port network:
                # 2*sqrt(Z0N)*bN=VN-Z0N*IN
                # The left hand side of the equation is realized with a (controlled) current
                # source with a gain of 2/sqrt(Z0N).
                f.write(f'F_b_{n + 1} a{n + 1} {ref_nodes[n]} V_c_{n + 1} {2.0/sqrt_Z0_n}\n')

                f.write(f'* Differential incident wave a sources for transfer from port {n + 1}\n')

                # CCVS and VCVS driving the transfer admittances with incident wave a = V/(2.0*sqrt(Z0)) + I*sqrt(Z0)/2
                #
                # These voltage sources in series realize the incident wave a. The types of the sources
                # and their gains arise from the definition of the incident wave a at port N:
                # aN=VN/(2*sqrt(Z0N)) + IN*sqrt(Z0N)/2
                # So we need a VCVS with a gain 1/(2*sqrt(Z0N)) in series with a CCVS with a gain sqrt(Z0N)/2
                f.write(f'H_p_{n + 1} nt_p_{n + 1} nts_p_{n + 1} V_p_{n + 1} {0.5*sqrt_Z0_n}\n')
                f.write(f'E_p_{n + 1} nts_p_{n + 1} {ref_nodes[n]} p{n + 1} {ref_nodes[n]} {1.0/(2.0*sqrt_Z0_n)}\n')

                # VCVS driving the transfer admittances with -a
                #
                # This source just copies the a wave and multiplies it by -1 to implement the negative side
                # of the differential a wave. The inversion of the sign is done by the connecting the source
                # in opposite direction to the reference node. Thus, the gain is 1.
                f.write(f'E_n_{n + 1} {ref_nodes[n]} nt_n_{n + 1} nt_p_{n + 1} {ref_nodes[n]} 1\n')

                f.write(f'* Current sensor on center node for transfer to port {n + 1}\n')

                # Current sensor for the transfer to current port
                f.write(f'V_c_{n + 1} nt_c_{n + 1} {ref_nodes[n]} 0\n')

                for j in range(self.network.nports):
                    f.write(f'* Transfer network from port {j + 1} to port {n + 1}\n')

                    # Stacking order in VectorFitting class variables:
                    # s11, s12, s13, ..., s21, s22, s23, ...
                    i_response = n * self.network.nports + j

                    # Get idx_pole_group and idx_pole_group_member for current response
                    idx_pole_group=self.map_idx_response_to_idx_pole_group[i_response]
                    idx_pole_group_member=self.map_idx_response_to_idx_pole_group_member[i_response]

                    # Start with proportional and constant term of the model
                    # H(s) = d + s * e  model
                    # Y(s) = G + s * C  equivalent admittance
                    g = self.constant[idx_pole_group][idx_pole_group_member]
                    c = self.proportional[idx_pole_group][idx_pole_group_member]

                    # R for constant term
                    if g < 0:
                        f.write(f'R{n + 1}_{j + 1} nt_n_{j + 1} nt_c_{n + 1} {np.abs(1 / g)}\n')
                    elif g > 0:
                        f.write(f'R{n + 1}_{j + 1} nt_p_{j + 1} nt_c_{n + 1} {1 / g}\n')

                    # C for proportional term
                    if c < 0:
                        f.write(f'C{n + 1}_{j + 1} nt_n_{j + 1} nt_c_{n + 1} {np.abs(c)}\n')
                    elif c > 0:
                        f.write(f'C{n + 1}_{j + 1} nt_p_{j + 1} nt_c_{n + 1} {c}\n')

                    # Get residues
                    residues = self.residues[idx_pole_group][idx_pole_group_member]

                    # Get poles
                    poles=self.poles[idx_pole_group]

                    # Transfer admittances represented by poles and residues
                    for idx_pole in range(len(poles)):
                        pole = poles[idx_pole]
                        residue = residues[idx_pole]
                        node = get_new_subckt_identifier()

                        if np.real(residue) < 0.0:
                            # Multiplication with -1 required, otherwise the values for RLC would be negative.
                            # This gets compensated by inverting the transfer current direction for this subcircuit
                            residue = -1 * residue
                            node += f' nt_n_{j + 1} nt_c_{n + 1}'
                        else:
                            node += f' nt_p_{j + 1} nt_c_{n + 1}'

                        if np.imag(pole) == 0.0:
                            # Real pole; Add rl_admittance
                            l = 1 / np.real(residue)
                            r = -1 * np.real(pole) / np.real(residue)
                            f.write(node + f' rl_admittance res={r} ind={l}\n')
                        else:
                            # Complex pole of a conjugate pair; Add rcl_vccs_admittance
                            r = -1 * np.real(pole) / np.real(residue)
                            c = 2 * np.real(residue) / (np.abs(pole) ** 2)
                            l = 1 / (2 * np.real(residue))
                            b = -2 * (np.real(residue) * np.real(pole) + np.imag(residue) * np.imag(pole))
                            gm = b * l * c
                            f.write(node + f' rcl_vccs_admittance res={r} cap={c} ind={l} gm={gm}\n')

            f.write(f'.ENDS {fitted_model_name}\n\n')

            # Subcircuit for an RLCG equivalent admittance of a complex-conjugate pole-residue pair
            f.write('.SUBCKT rcl_vccs_admittance n_pos n_neg res=1e3 cap=1e-9 ind=100e-12 gm=1e-3\n')
            f.write('L1 n_pos 1 {ind}\n')
            f.write('C1 1 2 {cap}\n')
            f.write('R1 2 n_neg {res}\n')
            f.write('G1 n_pos n_neg 1 2 {gm}\n')
            f.write('.ENDS rcl_vccs_admittance\n\n')

            # Subcircuit for an RL equivalent admittance of a real pole-residue pair
            f.write('.SUBCKT rl_admittance n_pos n_neg res=1e3 ind=100e-12\n')
            f.write('L1 n_pos 1 {ind}\n')
            f.write('R1 1 n_neg {res}\n')
            f.write('.ENDS rl_admittance\n\n')
