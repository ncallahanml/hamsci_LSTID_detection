import os
import warnings
import pickle
import numpy as np
import numpy.polynomial.polynomial as poly
import pandas as pd
import xarray as xr
import math
import datetime
from operator import itemgetter

import string

import statsmodels.api as sm
from scipy.interpolate import CubicSpline
from scipy.signal import butter, filtfilt
from scipy.ndimage import gaussian_filter
from scipy.optimize import curve_fit

################################################################################
# Nick's Edge Detection Code ###################################################
################################################################################

def occurrence_max(arr, n):
    """
    Selects the maximum value, excluding upper outliers, from the image array
    to provide a more consistent thresholding range

    Args:
    arr : `np.ndarray`
    - preprocessed input image
    - only tested with integer dtype

    n : `int`
    - number of upper (preprocessed) pixel values to exclude
    - the cumulative sum of the histogram (starting from the top side)
    is trimmed to this value when returning the maximum

    Returns:
    max_value : `int`
    - maximum value in `arr` after outliers
    """
    hist, bins = np.histogram(
        arr, 
        bins=np.arange(np.min(arr), np.max(arr) + 2)
    )
    bins = bins[1:]

    hist, bins = hist[::-1], bins[::-1]
    hist = np.cumsum(hist)
    bin_mask = hist >= n

    max_value = np.max(bins[bin_mask])
    return max_value

def rescale_to_int(arr, occurrence_n=100, i_max=30):
    """
    Rescales the preprocessed image array, which can be of a float dtype due
    to effects such as blurring, back into a formatted integer range

    This effectively determines the available thresholds, the minimum being
    zero and the maximum being near `i_max`, although there can be some deviation
    due to outlier effects

    Args:
    arr : `np.ndarray`
    - preprocessed input image
    - only tested with integer dtype

    Kwargs:
    occurence_n : `int`
    - upper number of pixels for thresholding
    - see function `occurence_max`'s argument `n`

    i_max : `int`
    - effective number of integer thresholds in output array
    - must be less than 255, as output is expected to have np.uint8 dtype
    
    Returns:
    arr : `np.ndarray`
    - rescaled `arr`
    """
    if i_max > 2**8 - 1:
        raise ValueError(
            f'`i_max` must be in 8bit unsigned integer range, not {i_max}'
        )
    if (arr_max := np.amax(arr.ravel())) > 2**16 - 1:
        raise ValueError(
            'All values in `arr` must be in 16bit unsigned integer range'
            + f' not {arr_max}'
        )

    arr = arr - np.amin(arr)
    max_val = occurrence_max(arr.round().astype(np.uint16), occurrence_n)
    factor = i_max / max_val
    arr = arr * factor
    if (arr_max := np.amax(arr.ravel())) > 2**8 - 1:
        raise ValueError(
            f'End rescaling max {arr_max} out of range of uint8 range'
            + ', considering adding explicit upper clipping'
        )
    arr = arr.round().astype(np.uint8)
    return arr

def stack_all_thresholds(
    arr, 
    select_min=True, 
    exact_thresh=False, 
    axis=0, 
    **rescale_kwargs,
):
    """
    Converts preprocessed array to integers and calculates all 
    thresholds, them combines the thresholds into a single edge

    Args:
    arr : `np.ndarray`
    - preprocessed input image
    - can be float or int dtype

    Kwargs:
    select_min : `bool`
    - chooses to select the minimum edge or maximum edge for each
    threshold
    - preprocessing system is tuned for selecting the minimum edge
    at the time of writing

    exact_thresh : `bool`
    - whether to select all pixels gte the threshold or just equal to
    - setting to False should provide smoother results, but the same
    index values may be selected multiple times across thresholds

    axis : `int`
    - numpy dimension for selection, should be 0 unless the 
    preprocessing routine changes

    Kwargs:
    rescale_kwargs : `dict`
    - kwargs sent into the rescaling function `rescale_to_int`

    Returns:
    thresh_edge_arr : `np.ndarray`
    - 2D stack of individual threshold arrays
    """
    arr = rescale_to_int(arr, **rescale_kwargs)

    thresholds = np.unique(arr)
    thresh_edges = list()
    for threshold in thresholds:
        if exact_thresh:
            thresh_mask = arr <= threshold
        else:
            thresh_mask = arr != threshold
        
        idx_fn = np.argmin if select_min else np.argmax
        thresh_edge = idx_fn(thresh_mask.astype(np.uint8), axis=axis, keepdims=True)
            
        if max(thresh_edge.shape) != max(arr.shape):
            raise ValueError(
                f'Expected largest dim in `thresh_edge` (shape {thresh_edge.shape})'
                + f'to match largest dim for `arr` (shape {arr.shape})'
            )
        
        thresh_edges.append(thresh_edge)
    thresh_edge_arr = np.concatenate(thresh_edges, axis=axis)
    return thresh_edge_arr

def lowess_smooth(arr, window_size=10, x=None):
    """
    LOWESS smoothing function, short wrapper around the
    statsmodels.parametric `lowess` function

    Args:
    arr : `np.ndarray`
    - 1D threshold from input image

    Kwargs:
    window_size : `int`
    - size of the window, in native array units, to apply the 
    LOWESS window over
    - converted to a fraction of the array for compatibility 
    with statsmodels

    x : `np.ndarray` or None
    - if not None, it must be a 1D array
    matching the length of `arr`
    - if None, `x` will be set as an evenly spaced array matching
    the size of `arr`
    - `x` can be passed for efficiency (not recreating the array)
    or when `arr` does not have all represented indices evenly
    spaced

    Returns:
    z : `np.ndarray`
    - LOWESS smoothed `arr`
    """
    if x is None:
        x = np.linspace(0, len(arr), len(arr))
    frac = window_size/len(arr)
    z = sm.nonparametric.lowess(arr, x, frac=frac, return_sorted=False)    
    return z

def smooth_remove_abs_deviation(arr, smooth_fn, max_abs_dev=20):
    """
    Smooths an array, calculates deviation from the smoothed array
    vs the original, filters out points of high deviation, and
    fills the high deviation points with interpolated values

    Args:
    arr : `np.ndarray`
    - 1D threshold from input image

    smooth_fn : `Callable`
    - smoothing function that takes only `arr` as a single argument
    
    Kwargs:
    max_abs_dev : `int`
    - maximum absolute deviation between the passed array and the
    smoothed counterpart before points get filtered

    Returns:
    z : `np.ndarray`
    - smoothed, filtered and interpolated version of `arr`
    """
    x = np.arange(0, arr.shape[0], 1)
    z = smooth_fn(arr)
    if len(x) != len(arr) or len(z) != len(x):
        raise ValueError(
            'Expected lengths of `arr`, `x`, and `z` to match : '
            + f'{len(arr)}, {len(x)}, {len(z)}'
        )
    dev_mask = np.abs(arr - z) < max_abs_dev
    interp = CubicSpline(x[dev_mask], z[dev_mask])
    z = interp(x)
    return z

def select_min_deviation(arrs, smooth_fn, max_abs_dev=20):
    """
    Selects a single edge array amongst multiple edge arrays, based
    on which array has the least standard deviation between the
    original array and the smoothed, filtered and interpolated 
    counterpart

    Args:
    arrs : `List[np.ndarray]`
    - list full of 1D edge arrays
    - for duck typing, can be any iterable or numpy array where
    simple iteration yields 1D edge arrays

    smooth_fn : `Callable`
    - function for smoothing each array in `arrs`
    - passed to `smooth_remove_abs_deviation`

    Kwargs:
    max_abs_dev : `int`
    - maximum absolute deviation for filtering a point in each arr
    in `arrs`
    - passed to `smooth_remove_abs_deviation`

    Returns:
    min_arrs : `Tuple[np.ndarray]`
    - tuple containing the selected arr from arrs as well as the
    smoothed, filtered and interpolated version of arr
    """
    min_arrs = None
    min_dev = np.inf
    for arr in arrs:
        z = smooth_remove_abs_deviation(arr, smooth_fn, max_abs_dev=max_abs_dev)
        dev = np.std(arr - z)
        if min_arrs is None or dev < min_dev:
            min_arrs = (arr, z)
            min_dev = dev
    return min_arrs

def take_quantile(thresh_arr, q):
    """
    Selects a single quantile from an array. Simple wrapper for
    `np.nanquantile` to clarify expected value of `q`

    Args:
    thresh_arr : `np.ndarray`
    - 2D stack of thresholds from an image

    q : `float` or `List[float]`
    - quantile to select from a passed distribution
    - passed to `np.nanquantile`

    Returns:
    line : `np.ndarray`
    - single edge from combined threshold arrays
    """
    if not isinstance(q, float):
        raise TypeError(
            f'Expected float for `q`, recieved {type(q)}'
        )
    if 0 >= q >= 1:
        raise ValueError(
            f'Expected `q` to be between 0 and 1, noninclusive, not {q}'
        )
    
    line = np.nanquantile(thresh_arr, q, axis=0)
    return line

def measure_thresholds(arr, qs=.8, lower_cutoff=10, **threshold_kwargs):
    """
    Calculates multiple thresholds, stacks them together, filters some
    values based on y axis cutoff, then selects a single value for
    each column from the remaining threshold distribution for each
    column

    Args:
    arr : `np.ndarray`
    - 2D input image to detect edge from
    - passed directly to `stack_all_thresholds`

    Kwargs:
    qs : `float` or `Iterable[float]`
    - quantile(s) to take from stacked thresholds
    - each q is passed to `take_quantile`

    lower_cutoff : `int`
    - minimum y axis value for which detected thresholds will allowed
    - anything lower than this is set to `np.nan` and implicitly
    removed when the column-wise quantile is taken

    threshold_kwargs : `dict`
    - keyword arguments passed to `stack_all_thresholds`

    Returns:
    med_lines : `List[np.ndarray]`
    - detected edges for each passed quantile value

    min_line : `np.ndarray`
    - single line from `med_lines` selected by `select_min_deviation`

    minz_line : `np.ndarray`
    - smoothed version of `min_line`
    """
    thresh_edge_arr = stack_all_thresholds(arr, **threshold_kwargs)
    
    thresh_edge_arr = thresh_edge_arr.astype(np.float32)
    thresh_edge_arr[thresh_edge_arr < lower_cutoff] = np.nan   
    
    # qs must be an iterable of floats
    if isinstance(qs, float):
        qs = [qs]

    med_lines = [take_quantile(thresh_edge_arr, q) for q in qs]
    min_line, minz_line = select_min_deviation(med_lines, lowess_smooth)
    
    return med_lines, min_line, minz_line

################################################################################
# Nathaniel and Diego's Sin Fitting Code #######################################
################################################################################

def scale_km(edge,ranges):
    """
    Scale detected edge array indices to kilometers.
    edge:   Edge in array indices.
    ranges: Ground range vector in km of histogram array.
    """
    ranges  = np.array(ranges) 
    edge_km = (edge / len(ranges) * ranges.ptp()) + ranges.min()

    return edge_km

def islandinfo(y, trigger_val, stopind_inclusive=True):
    """
    From https://stackoverflow.com/questions/50151417/numpy-find-indices-of-groups-with-same-value
    """
    # Setup "sentients" on either sides to make sure we have setup
    # "ramps" to catch the start and stop for the edge islands
    # (left-most and right-most islands) respectively
    y_ext = np.r_[False,y==trigger_val, False]

    # Get indices of shifts, which represent the start and stop indices
    idx = np.flatnonzero(y_ext[:-1] != y_ext[1:])

    # Lengths of islands if needed
    lens = idx[1::2] - idx[:-1:2]

    # Using a stepsize of 2 would get us start and stop indices for each island
    return list(zip(idx[:-1:2], idx[1::2]-int(stopind_inclusive))), lens

def sinusoid(tt_sec,T_hr,amplitude_km,phase_hr,offset_km,slope_kmph):
    """
    Sinusoid function that will be fit to data.
    """
    phase_rad       = (2.*np.pi) * (phase_hr / T_hr) 
    freq            = 1./(datetime.timedelta(hours=T_hr).total_seconds())
    result          = np.abs(amplitude_km) * np.sin( (2*np.pi*tt_sec*freq ) + phase_rad ) + (slope_kmph/3600.)*tt_sec + offset_km
    return result

def bandpass_filter(
    data,
    lowcut=0.00005556, 
    highcut=0.0001852, 
    fs=0.0166666666666667, 
    order=4):
    """
    Defaults:
    1 hour period = 0.000277777778 Hz
    5 hour period   = 0.00005556 Hz
    Sampling Freq   = 0.0166666666666667 Hz (our data is in 1 min resolution)
    """
    nyquist = 0.5 * fs
    low = lowcut / nyquist
    high = highcut / nyquist
    b, a = butter(order, [low, high], btype='band')
    filtered = filtfilt(b, a, data)
    return filtered

def run_edge_detect(
    date,
    heatmaps,
    x_trim          = .08333,
    y_trim          = .08,
    sigma           = 4.2, # 3.8 was good # Gaussian filter kernel
    qs              = [.4, .5, .6],
    occurence_n     = 60,
    i_max           = 30,
    cache_dir       = 'cache',
    bandpass        = True,
    lstid_T_hr_lim  = (1, 4.5),
    lstid_criteria  = {},
    **kwArgs):
    """
    bandpass:   Apply a bandpass filter after detrending but before sin fitting.

    lstid_T_hr_lim: Values used for cutoff of the bandpass filter and for 
                    LSTID classification (unless changed with lstid_criteria
                    dictionary.

    lstid_criteria: Dictionary used for classifying a day as LSTID-active.
        The following values are used if not explicity specified:
            lstid_criteria['T_hr']          = lstid_T_hr_lim    
            lstid_criteria['amplitude_km']  = (20,2000)
            lstid_criteria['r2']            = (0.35,1.1)
    """

    arr = heatmaps.get_date(date,raise_missing=False)

    if arr is None:
        warnings.warn(f'Date {date} has no input')
        return
        
    xl_trim, xrt_trim   = x_trim if isinstance(x_trim, (tuple, list)) else (x_trim, x_trim)
    yl_trim, yr_trim    = x_trim if isinstance(y_trim, (tuple, list)) else (y_trim, y_trim)
    xrt, xl = math.floor(xl_trim * arr.shape[0]), math.floor(xrt_trim * arr.shape[0])
    yr, yl  = math.floor(yl_trim * arr.shape[1]), math.floor(yr_trim * arr.shape[1])

    arr = arr[xrt:-xl, yr:-yl]

    ranges_km   = arr.coords['height']
    arr_times   = [date + x for x in pd.to_timedelta(arr.coords['time'])]
    Ts          = np.mean(np.diff(arr_times)) # Sampling Period

    arr     = np.nan_to_num(arr, nan=0)

    arr = gaussian_filter(arr.T, sigma=(sigma, sigma))  # [::-1,:]
    med_lines, min_line, minz_line = measure_thresholds(
        arr,
        qs=qs, 
        occurrence_n=occurence_n, 
        i_max=i_max
    )

    med_lines   = [scale_km(x,ranges_km) for x in med_lines]
    min_line    = scale_km(min_line,ranges_km)
    minz_line   = scale_km(minz_line,ranges_km)

    med_lines   = pd.DataFrame(
        np.array(med_lines).T,
        index=arr_times,
        columns=qs,
    ).reset_index(names='Time')

    edge_0  = pd.Series(min_line.squeeze(), index=arr_times, name=date)
    edge_0  = edge_0.interpolate()
    edge_0  = edge_0.fillna(0.)

    # X-Limits for plotting
    x_0     = date + datetime.timedelta(hours=12)
    x_1     = date + datetime.timedelta(hours=24)
    xlim    = (x_0, x_1)

    # Window Limits for FFT analysis.
    win_0   = date + datetime.timedelta(hours=13)
    win_1   = date + datetime.timedelta(hours=23)
    winlim  = (win_0, win_1)

    # Select data in analysis window.
    tf      = np.logical_and(edge_0.index >= win_0, edge_0.index < win_1)
    edge_1  = edge_0[tf]

    times_interp  = [x_0]
    while times_interp[-1] < x_1:
        times_interp.append(times_interp[-1] + Ts)

    x_interp    = [pd.Timestamp(x).value for x in times_interp]
    xp_interp   = [pd.Timestamp(x).value for x in edge_1.index]
    interp      = np.interp(x_interp,xp_interp,edge_1.values)
    edge_1      = pd.Series(interp,index=times_interp,name=date)
    
    sg_edge     = edge_1.copy()
    tf = np.logical_and(sg_edge.index >= winlim[0], sg_edge.index < winlim[1])
    sg_edge[~tf] = 0

    # Curve Fit Data ############################################################### 

    # Convert Datetime Objects to Relative Seconds and pull out data
    # for fitting.
    t0      = datetime.datetime(date.year,date.month,date.day)
    tt_sec  = np.array([x.total_seconds() for x in (sg_edge.index - t0)])
    data    = sg_edge.values

    # Calculate the rolling Coefficient of Variation and use as a stability parameter
    # to determine the start and end time of good edge detection.
    roll_win    = 15 # 15 minute rolling window
    xx_n = edge_1.rolling(roll_win).std()
    xx_d = edge_1.rolling(roll_win).mean()
    stability   = xx_n/xx_d # Coefficient of Varation

    stab_thresh = 0.05 # Require Coefficient of Variation to be less than 0.05
    tf  = stability < stab_thresh

    # Find 'islands' (aka continuous time windows) that meet the stability criteria
    islands, island_lengths  = islandinfo(tf,1)

    # Get the longest continuous time window meeting the stability criteria.
    isl_inx = np.argmax(island_lengths)
    island  = islands[isl_inx]
    sInx    = island[0]
    eInx    = island[1]

    fitWin_0    = edge_1.index[sInx]
    fitWin_1    = edge_1.index[eInx]
    
    # We know that the edges are very likely to have problems,
    # even if they meet the stability criteria. So, we require
    # the fit boundaries to be at minimum 30 minutes after after
    # and before the start and end times.
    margin = datetime.timedelta(minutes=30)
    if fitWin_0 < (win_0 + margin):
        fitWin_0 = win_0 + margin

    if fitWin_1 > (win_1 - margin):
        fitWin_1 = win_1 - margin

    # Select the data and times to be used for curve fitting.
    fitWinLim   = (fitWin_0, fitWin_1)
    tf          = np.logical_and(sg_edge.index >= fitWin_0, sg_edge.index < fitWin_1)
    fit_times   = sg_edge.index[tf].copy()
    tt_sec      = tt_sec[tf]
    data        = data[tf]

    # now do the fit
    try:
        # Curve Fit 2nd Deg Polynomial #########  
        coefs, [ss_res, rank, singular_values, rcond] = poly.polyfit(tt_sec, data, 2, full = True)
        ss_res_poly_fit = ss_res[0]
        poly_fit = poly.polyval(tt_sec, coefs)
        poly_fit = pd.Series(poly_fit,index=fit_times)

        p0_poly_fit = {}
        for cinx, coef in enumerate(coefs):
            p0_poly_fit[f'c_{cinx}'] = coef

        ss_tot_poly_fit      = np.sum( (data - np.mean(data))**2 )
        r_sqrd_poly_fit      = 1 - (ss_res_poly_fit / ss_tot_poly_fit)
        p0_poly_fit['r2']    = r_sqrd_poly_fit

        # Detrend Data Using 2nd Degree Polynomial
        data_detrend         = data - poly_fit

        # Apply bandpass filter
        lowcut  = 1/(lstid_T_hr_lim[1]*3600) # higher period limit 
        highcut = 1/(lstid_T_hr_lim[0]*3600) # lower period limit
        fs      = 1/60
        order   = 4
        
        filtered_signal  = bandpass_filter(data=data_detrend.values, lowcut=lowcut, highcut=highcut, fs=fs, order=order)
        filtered_detrend = pd.Series(data=filtered_signal, index=data_detrend.index)

        if bandpass == True:
            data_detrend = filtered_detrend

        T_hr_guesses = np.arange(1,4.5,0.5)
        
        all_sin_fits = []
        for T_hr_guess in T_hr_guesses:
            # Curve Fit Sinusoid ################### 
            guess = {}
            guess['T_hr']           = T_hr_guess
            guess['amplitude_km']   = np.ptp(data_detrend)/2.
            guess['phase_hr']       = 0.
            guess['offset_km']      = np.mean(data_detrend)
            guess['slope_kmph']     = 0.

            try:
                sinFit,pcov,infodict,mesg,ier = curve_fit(sinusoid, tt_sec, data_detrend, p0=list(guess.values()),full_output=True)
            except:
                continue

            p0_sin_fit = {}
            p0_sin_fit['T_hr']           = sinFit[0]
            p0_sin_fit['amplitude_km']   = np.abs(sinFit[1])
            p0_sin_fit['phase_hr']       = sinFit[2]
            p0_sin_fit['offset_km']      = sinFit[3]
            p0_sin_fit['slope_kmph']     = sinFit[4]

            sin_fit = sinusoid(tt_sec, **p0_sin_fit)
            sin_fit = pd.Series(sin_fit,index=fit_times)

            # Calculate r2 for Sinusoid Fit
            ss_res_sin_fit              = np.sum( (data_detrend - sin_fit)**2)
            ss_tot_sin_fit              = np.sum( (data_detrend - np.mean(data_detrend))**2 )
            r_sqrd_sin_fit              = 1 - (ss_res_sin_fit / ss_tot_sin_fit)
            p0_sin_fit['r2']            = r_sqrd_sin_fit
            p0_sin_fit['T_hr_guess']    = T_hr_guess

            all_sin_fits.append(p0_sin_fit)
    except:
        all_sin_fits = []

    if len(all_sin_fits) > 0:
        all_sin_fits = sorted(all_sin_fits, key=itemgetter('r2'), reverse=True)

        # Pick the best fit sinusoid.
        p0_sin_fit                  = all_sin_fits[0]
        p0                          = p0_sin_fit.copy()
        all_sin_fits[0]['selected'] = True
        del p0['r2']
        del p0['T_hr_guess']
        sin_fit     = sinusoid(tt_sec, **p0)
        sin_fit     = pd.Series(sin_fit,index=fit_times)
    else:
        sin_fit     = pd.Series(np.zeros(len(fit_times))*np.nan,index=fit_times)
        p0_sin_fit  = {}

        poly_fit    = sin_fit.copy()
        p0_poly_fit = {}

        data_detrend = sin_fit.copy()

    # Classification
    if 'T_hr' not in lstid_criteria:
        lstid_criteria['T_hr']          = lstid_T_hr_lim    
    if 'amplitude_km' not in lstid_criteria:
        lstid_criteria['amplitude_km']  = (20,2000)
    if 'r2' not in lstid_criteria:
        lstid_criteria['r2']            = (0.35,1.1)
    
    if p0_sin_fit != {}:
        crits   = []
        for key, crit in lstid_criteria.items():
            val     = p0_sin_fit[key]
            result  = np.logical_and(val >= crit[0], val < crit[1])
            crits.append(result)
        p0_sin_fit['is_lstid']  = np.all(crits)

    # Package SpotArray into XArray
    daDct               = {}
    daDct['data']       = arr
    daDct['coords']     = coords = {}
    coords['ranges_km'] = ranges_km.values
    coords['datetimes'] = arr_times
    spotArr             = xr.DataArray(**daDct)

    # Set things up for data file.
    result  = {}
    result['spotArr']           = spotArr
    result['med_lines']         = med_lines
    result['000_detectedEdge']  = edge_0
    result['001_windowLimits']  = edge_1
    result['003_sgEdge']        = sg_edge
    result['sin_fit']           = sin_fit
    result['p0_sin_fit']        = p0_sin_fit
    result['poly_fit']          = poly_fit
    result['p0_poly_fit']       = p0_poly_fit
    result['stability']         = stability
    result['data_detrend']      = data_detrend
    result['all_sin_fits']      = all_sin_fits

    result['metaData']          = meta  = {}
    meta['date']                = date
    meta['x_trim']              = x_trim
    meta['y_trim']              = y_trim
    meta['sigma']               = sigma
    meta['qs']                  = qs
    meta['occurence_n']         = occurence_n
    meta['i_max']               = i_max
    meta['xlim']                = xlim
    meta['winlim']              = winlim
    meta['fitWinLim']           = fitWinLim
    meta['lstid_criteria']      = lstid_criteria

    return result
