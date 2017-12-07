from copy import deepcopy

import numpy as np
import matplotlib.pyplot as plt
from scipy.optimize import minimize
from scipy.interpolate import interp1d
from astropy import wcs
import astropy.io.fits as pf

from ifscube import stats
import ifscube.spectools as st
import ifscube.elprofile as lprof


def scale_bounds(bounds, scale_factor, npars_pc):

    b = deepcopy(bounds)
    for j in b[::npars_pc]:
        for k in (0, 1):
            if j[k] is not None:
                j[k] /= scale_factor

    return b


class Spectrum():

    def __init__(self, *args, **kwargs):

        if len(args) > 0:
            self.__load__(*args, **kwargs)

    def __load__(self, fname, ext=0, redshift=0, variance=None,
                 flags=None):

        with pf.open(fname) as hdu:
            self.data = hdu[ext].data
            self.header = hdu[ext].header
            self.wcs = wcs.WCS(self.header)

        if variance is not None:
            assert variance.shape == self.data.shape, 'Variance spectrum must'\
                ' have the same shape of the spectrum itself.'
            self.variance = variance
        else:
            self.variance = np.ones_like(self.data)

        if flags is not None:
            assert flags.shape == self.data.shape, 'Flag spectrum must have '\
                'the same shape of the spectrum itself.'
            self.flags = flags
        else:
            self.flags = np.zeros_like(self.data)

        self.wl = self.wcs.wcs_pix2world(np.arange(len(self.data)), 0)[0]
        self.delta_lambda = self.wcs.pixel_scale_matrix[0, 0]

        if redshift != 0:
            self.restwl = self.__dopcor__()
        else:
            self.restwl = self.wl

    def __dopcor__(self):

        self.restwl = self.wl / (1. + self.redshift)

    def linefit(self, p0, function='gaussian', fitting_window=None,
                writefits=False, outimage=None, variance=None,
                constraints=(), bounds=None, inst_disp=1.0,
                min_method='SLSQP', minopts={'eps': 1e-3}, copts=None,
                weights=None):
        """
        Fits a spectral feature with a gaussian function and returns a
        map of measured properties. This is a wrapper for the scipy
        minimize function that basically iterates over the cube,
        has a formula for the reduced chi squared, and applies
        an internal scale factor to the flux.

        Parameters
        ----------
        p0 : iterable
            Initial guess for the fitting funcion, consisting of a list
            of 3N parameters for N components of **function**. In the
            case of a gaussian fucntion, these parameters must be given
            as [amplitude0, center0, sigma0, amplitude1, center1, ...].
        function : string
            The function to be fitted to the spectral features.
            Available options and respective parameters are:
                'gaussian' : amplitude, central wavelength in angstroms,
                    sigma in angstroms
                'gauss_hermite' : amplitude, central wavelength in
                    angstroms, sigma in angstroms, h3 and h4
        fitting_window : iterable
            Lower and upper wavelength limits for the fitting
            algorithm. These limits should allow for a considerable
            portion of continuum besides the desired spectral features.
        writefits : boolean
            Writes the results in a FITS file.
        outimage : string
            Name of the FITS file in which to write the results.
        variance : float, 1D, 2D or 3D array
            The variance of the flux measurments. It can be given
            in one of four formats. If variance is a float it is
            applied as a contant to the whole spectrum. If given as 1D
            array it assumed to be a spectrum that will be applied to
            the whole cube. As 2D array, each spaxel will be applied
            equally to all wavelenths. Finally the 3D array must
            represent the variance for each elemente of the data cube.
            It defaults to None, in which case it does not affect the
            minimization algorithm, and the returned Chi2 will be in
            fact just the fit residuals.
        inst_disp : number
            Instrumental dispersion in pixel units. This argument is
            used to evaluate the reduced chi squared. If let to default
            it is assumed that each wavelength coordinate is a degree
            of freedom. The physically sound way to do it is to use the
            number of dispersion elements in a spectrum as the degrees
            of freedom.
        bounds : sequence
            Bounds for the fitting algorithm, given as a list of
            [xmin, xmax] pairs for each x parameter.
        constraints : dict or sequence of dicts
            See scipy.optimize.minimize
        min_method : string
            Minimization method. See scipy.optimize.minimize.
        minopts : dict
            Dictionary of options to be passed to the minimization
            routine. See scipy.optimize.minimize.
        individual_spec : False or x,y pair
            Pixel coordinates for the spectrum you wish to fit
            individually.
        copts : dict
            Arguments to be passed to the spectools.continuum function.
        refit : boolean
            Use parameters from nearby sucessful fits as the initial
            guess for the next fit.
        update_bounds : boolean
            If using refit, update the bounds for the next fit.
        bound_range : number
            Fractional difference for updating the bounds when using refit.
        spiral_loop : boolean
            Begins the fitting with the central spaxel and continues
            spiraling outwards.
        spiral_center : iterable
            Central coordinates for the beginning of the spiral given
            as a list of two coordinates [x0, y0]
        fit_continuum : boolean
            If True fits the continuum just before attempting to fit
            the emission lines. Setting this option to False will
            cause the algorithm to look for self.cont, which should
            contain a data cube of continua.

        Returns
        -------
        sol : numpy.ndarray
            A data cube with the solution for each spectrum occupying
            the respective position in the image, and each position in
            the first axis giving the different parameters of the fit.

        See also
        --------
        scipy.optimize.curve_fit, scipy.optimize.leastsq
        """

        # Sets a pre-made nan vector for nan solutions.
        npars = len(p0)
        nan_solution = np.array([np.nan for i in range(npars + 1)])

        if function == 'gaussian':
            fit_func = lprof.gauss
            self.fit_func = lprof.gauss
            npars_pc = 3
            self.parnames = ('A', 'wl', 's')
        elif function == 'gauss_hermite':
            fit_func = lprof.gausshermite
            self.fit_func = lprof.gausshermite
            npars_pc = 5
            self.parnames = ('A', 'wl', 's', 'h3', 'h4')
        else:
            raise NameError('Unknown function "{:s}".'.format(function))

        if fitting_window is None:
            fw = np.ones_like(self.data).astype('bool')
        else:
            fw = (self.wl > fitting_window[0]) &\
                 (self.wl < fitting_window[1])

        if copts is None:
            copts = {
                'niterate': 5, 'degr': 4, 'upper_threshold': 2,
                'lower_threshold': 2}

        copts.update(dict(returns='function'))

        #
        # Avoids fit if more than 80% of the pixels are flagged.
        #
        if np.sum(self.flags) > 0.8 * self.flags.size:
            p = nan_solution
            self.fit_status = 98
            return

        valid_pixels = (self.flags == 0) & fw

        wl = deepcopy(self.restwl[valid_pixels])
        data = deepcopy(self.data[valid_pixels])

        if weights is None:
            weights = np.ones_like(data)

        sol = np.zeros((npars + 1,))
        self.fitcont = np.zeros_like(data)
        self.fitwl = wl
        self.fitspec = np.zeros_like(data)
        self.resultspec = np.zeros_like(data)

        #
        # Pseudo continuum fitting.
        #
        try:
            cont = self.cont
        except AttributeError:
            cont = st.continuum(wl, data, **copts)[1]
        self.fitcont = cont

        #
        # Short alias for the spectrum that is going to be fitted.
        #
        s = data - cont
        w = weights
        v = self.variance[valid_pixels]

        #
        # Scaling the flux in the spectrum, the initial guess and the
        # bounds to bring everything close to unity.
        #
        scale_factor = np.mean(s)
        if scale_factor <= 0:
            p = nan_solution
            fit_status = 97
            return
        s /= scale_factor
        if not np.all(v == 1.):
            v /= scale_factor ** 2
        p0[::npars_pc] /= scale_factor
        sbounds = scale_bounds(bounds, scale_factor, npars_pc)

        #
        # Here the actual fit begins
        #
        def res(x):
            m = fit_func(self.fitwl, x)
            # Should I divide this by the sum of the weights?
            a = w * (s - m) ** 2
            b = a / v
            rms = np.sqrt(np.sum(b))
            return rms

        r = minimize(res, x0=p0, method=min_method, bounds=sbounds,
                     constraints=constraints, options=minopts)

        if r.status != 0:
            print(r.message, r.status)
        # Reduced chi squared of the fit.
        chi2 = res(r['x'])
        nu = len(s)/inst_disp - npars - len(constraints) - 1
        red_chi2 = chi2 / nu
        p = np.append(r['x'], red_chi2)
        fit_status = r.status

        p[::npars_pc] *= scale_factor
        sol = p
        self.fitspec = s * scale_factor + cont
        self.resultspec = cont + fit_func(self.fitwl, r['x']) * scale_factor

        self.em_model = sol
        self.fit_status = fit_status

        if writefits:

            # Basic tests and first header
            if outimage is None:
                outimage = self.fitsfile.replace('.fits',
                                                 '_linefit.fits')
            hdr = deepcopy(self.header)

            # Creates MEF output.
            h = pf.HDUList()
            h.append(pf.PrimaryHDU(header=hdr))

            # Creates the fitted spectrum extension
            hdr = pf.Header()
            hdr['object'] = ('spectrum', 'Data in this extension')
            hdr['CRPIX3'] = (1, 'Reference pixel for wavelength')
            hdr['CRVAL3'] = (wl[0], 'Reference value for wavelength')
            hdr['CD3_3'] = (np.average(np.diff(wl)), 'CD3_3')
            h.append(pf.ImageHDU(data=self.fitspec, header=hdr))

            # Creates the fitted continuum extension.
            hdr['object'] = 'continuum'
            h.append(pf.ImageHDU(data=self.fitcont, header=hdr))

            # Creates the fitted function extension.
            hdr['object'] = 'fit'
            h.append(pf.ImageHDU(data=self.resultspec, header=hdr))

            # Creates the solution extension.
            hdr['object'] = 'parameters'
            hdr['function'] = (function, 'Fitted function')
            hdr['nfunc'] = (len(p)/3, 'Number of functions')
            h.append(pf.ImageHDU(data=sol, header=hdr))

            # Creates the minimize's exit status extension
            hdr['object'] = 'status'
            h.append(pf.ImageHDU(data=fit_status, header=hdr))

            h.writeto(outimage)

        return sol

    def fit_uncertainties(self, snr=10):

        if self.fit_func == lprof.gauss:

            peak = self.em_model[0]
            flux = self.em_model[0] * self.em_model[2] * np.sqrt(2. * np.pi)

        elif self.fit_func == lprof.gausshermite:

            # This is a crude estimate for the peak, and only works
            # for small values of h3 and h4.

            peak = self.em_model[0] / self.em_model[2] / np.sqrt(2. * np.pi)
            flux = self.em_model[0]

        try:
            s_peak = interp1d(self.wl, self.err)(self.em_model[1])
        except AttributeError:
            s_peak = peak / snr

        # The third element in em_model is always the sigma
        fwhm = self.em_model[2] * 2. * np.sqrt(2. * np.log(2.))

        sf = stats.line_flux_error(flux, fwhm, self.delta_lambda, peak, s_peak)

        self.flux_err = sf

    def loadfit(self, fname):
        """
        Loads the result of a previous fit, and put it in the
        appropriate variables for the plotfit function.

        Parameters
        ----------
        fname : string
            Name of the FITS file generated by gmosdc.linefit.

        Returns
        -------
        Nothing.
        """

        self.fitwl = st.get_wl(fname, pix0key='crpix0', wl0key='crval0',
                               dwlkey='cd1_1', hdrext=1, dataext=1)
        self.fitspec = pf.getdata(fname, ext=1)
        self.fitcont = pf.getdata(fname, ext=2)
        self.resultspec = pf.getdata(fname, ext=3)

        self.em_model = pf.getdata(fname, ext=4)
        self.fit_status = pf.getdata(fname, ext=5)

        fit_info = {}
        func_name = pf.getheader(fname, ext=4)['function']
        fit_info['function'] = func_name

        if func_name == 'gaussian':
            self.fit_func = lprof.gauss
            npars = 3

        if func_name == 'gauss_hermite':
            self.fit_func = lprof.gausshermite
            npars = 5

        fit_info['parameters'] = npars
        fit_info['components'] = (self.em_model.shape[0] - 1) / npars

        self.fit_info = fit_info

    def plotfit(self, show=True, axis=None, output='stdout'):
        """
        Plots the spectrum and features just fitted.

        Parameters
        ----------
        x : number
            Horizontal coordinate of the desired spaxel.
        y : number
            Vertical coordinate of the desired spaxel.

        Returns
        -------
        Nothing.
        """

        if axis is None:
            fig = plt.figure(1)
            plt.clf()
            ax = fig.add_subplot(111)
        else:
            ax = axis

        p = self.em_model[:-1]
        c = self.fitcont
        wl = self.fitwl
        f = self.fit_func
        s = self.fitspec

        ax.plot(wl, c + f(wl, p))
        ax.plot(wl, c)
        ax.plot(wl, s)

        if self.fit_func == lprof.gauss:
            npars = 3
            parnames = ('A', 'wl', 's')
        elif self.fit_func == lprof.gausshermite:
            npars = 5
            parnames = ('A', 'wl', 's', 'h3', 'h4')
        else:
            raise NameError('Unkown fit function.')

        if len(p) > npars:
            for i in np.arange(0, len(p), npars):
                ax.plot(wl, c + f(wl, p[i: i+npars]), 'k--')

        pars = (npars * '{:10s}' + '\n').format(*parnames)
        for i in np.arange(0, len(p), npars):
            pars += (
                ('{:10.2e}' + (npars-1) * '{:10.2f}' + '\n')
                .format(*p[i:i+npars]))

        if show:
            plt.show()

        if output == 'stdout':
            print(pars)
        if output == 'return':
            return pars

        return

    def plotspec(self, overplot=True):

        if overplot:
            try:
                ax = plt.gca()
            except:
                fig = plt.figure()
                ax = fig.add_subplot(111)

        ax.plot(self.wl, self.data)
        plt.show()
