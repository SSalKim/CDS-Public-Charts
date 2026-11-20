"""Selective display filtering of complete closed contours, never terrain holes."""
import numpy as np
from scipy.ndimage import gaussian_filter

from . import policy

GRAVITY = 9.80665
QUALITY_VERSION = 3
TIBET_BOUNDS = (25, 40, 70, 105)


# Reduced MSLP is unreliable where the surface is high: the reduction extrapolates through a
# fictitious air column, producing grid-scale bulls-eyes over the Tibetan Plateau. KMA charts keep
# the model's values but draw broad, smooth isobars there. Blend towards a 250 km Gaussian mean
# as terrain rises from 0.7 to 2 km (Mongolian, Qaidam and Qilian ranges included). The Korea map has no such
# plateau and is never smoothed, so its mountain isobars stay exactly as published.
TERRAIN_SMOOTH_LOW, TERRAIN_SMOOTH_HIGH, TERRAIN_SMOOTH_KM = 700., 2000., 250.

# From the smoothing cutover the extra width depends on the model. KMA draws KIM with ~30 km
# overall smoothing plus ~75 km over high terrain (line positions fitted on 12 cases, pixel-aligned).
# Each model gets the width that leaves the same small-scale roughness over Tibet as KMA's KIM
# (0.51 hPa after the 30 km contour smoothing; 2 cycles x 6 leads per model, 2026-11-19/20).
MODEL_TERRAIN_KM = {"ukmo": 75, "ecmwf": 100, "aifs": 100, "icon": 100, "gfs": 175, "aigfs": 175, "cmc": 175}
# Low levels extrapolated below the terrain get the same width, fading in from 300 m above the
# level's height to 1 km below it.
BELOW_GROUND_LEVELS = (925, 850, 700)
BELOW_GROUND_MARGIN, BELOW_GROUND_RAMP = 300., 1000.


def _grid_steps(latitude, longitude):
    dy = float(np.median(np.abs(np.diff(latitude)))) * 111
    dx = float(np.median(np.abs(np.diff(longitude)))) * 111 * np.cos(np.deg2rad(35))
    return dy, dx


def _smooth_finite(value, km, latitude, longitude):
    dy, dx = _grid_steps(latitude, longitude)
    value = np.asarray(value, dtype=float)
    finite = np.isfinite(value)
    filled = np.where(finite, value, np.nanmean(value) if finite.any() else 0.)
    return gaussian_filter(filled, (km / dy, km / dx), mode="nearest"), finite


def terrain_weighted_pressure(pressure, terrain, latitude, longitude, km=TERRAIN_SMOOTH_KM):
    weight = np.clip((terrain - TERRAIN_SMOOTH_LOW) / (TERRAIN_SMOOTH_HIGH - TERRAIN_SMOOTH_LOW), 0, 1)
    if not np.any(weight):
        return pressure
    smooth, finite = _smooth_finite(pressure, km, latitude, longitude)
    return np.where(finite, (1 - weight) * pressure + weight * smooth, pressure)


def below_ground_levels(arrays, km):
    """Smooth every 925-700 hPa field where its level lies below the terrain."""
    terrain = arrays.get("terrain_height")
    out = arrays
    for level in BELOW_GROUND_LEVELS:
        height_key = f"geopotential_{level}"
        if terrain is None or height_key not in arrays or np.shape(arrays[height_key]) != np.shape(terrain):
            continue
        weight = np.clip((terrain - np.asarray(arrays[height_key]) / GRAVITY + BELOW_GROUND_MARGIN) / BELOW_GROUND_RAMP, 0, 1)
        if not np.any(weight):
            continue
        out = dict(out) if out is arrays else out
        for key in arrays:
            if key.endswith(f"_{level}") and np.shape(arrays[key]) == np.shape(terrain):
                smooth, finite = _smooth_finite(arrays[key], km, arrays["latitude"], arrays["longitude"])
                out[key] = np.where(finite, (1 - weight) * np.asarray(arrays[key], dtype=float) + weight * smooth, arrays[key])
    return out


def display_fields(arrays, cycle, domain="asia", model_id=None):
    """Fields as drawn: from the KMA-style cutover, Asia MSLP is terrain-smoothed for every model.

    From the smoothing cutover the width is per model and the 925-700 hPa fields are also smoothed
    below ground. The Korea map is never smoothed here.
    """
    if domain != "asia" or not policy.kma_style(cycle) or "terrain_height" not in arrays:
        return arrays
    per_model = policy.model_smoothing(cycle) and model_id in MODEL_TERRAIN_KM
    km = MODEL_TERRAIN_KM[model_id] if per_model else TERRAIN_SMOOTH_KM
    out = arrays
    if "pressure_msl" in arrays and np.shape(arrays["pressure_msl"]) == np.shape(arrays["terrain_height"]):
        smoothed = terrain_weighted_pressure(arrays["pressure_msl"], arrays["terrain_height"],
                                             arrays["latitude"], arrays["longitude"], km)
        if smoothed is not arrays["pressure_msl"]:
            # The published field is kept for centre detection over lowlands, where both are identical.
            out = {**arrays, "pressure_msl": smoothed, "pressure_msl_published": arrays["pressure_msl"]}
    return below_ground_levels(out, km) if per_model else out


def terrain_masks(arrays):
    # Preserve the old call contract; no altitude or below-ground field is hidden.
    return {}


def mask_statistics(masks):
    return {str(key): {"masked_cells": int(np.count_nonzero(~value)), "total_cells": int(value.size)}
            for key,value in masks.items()}


def closed_contour_predicate(arrays, cycle=None):
    # From the smoothing cutover the smoothed fields are drawn whole, like KMA's plateau rings and centres.
    if cycle is not None and policy.model_smoothing(cycle):
        return None
    latitude,longitude=arrays["latitude"],arrays["longitude"]
    terrain=arrays["terrain_height"]
    gradients={}
    def suspicious(points,level,kind):
        if kind!="pressure" and kind not in (700,850,925):
            return False
        points=np.asarray(points)
        south,north,west,east=TIBET_BOUNDS
        inside=((points[:,1]>=south)&(points[:,1]<=north)&(points[:,0]>=west)&(points[:,0]<=east))
        rows=np.clip(np.searchsorted(latitude,points[:,1]),0,len(latitude)-1)
        cols=np.clip(np.searchsorted(longitude,points[:,0]),0,len(longitude)-1)
        highland=inside&(terrain[rows,cols]>=3000)
        if np.mean(highland)<.6:
            return False
        if kind not in gradients:
            value=arrays["pressure_msl"]/100 if kind=="pressure" else arrays[f"geopotential_{kind}"]/GRAVITY
            dy,dx=np.gradient(value,latitude*111,longitude*111)
            gradients[kind]=np.hypot(dy,dx/np.maximum(.1,np.cos(np.deg2rad(latitude[:,None]))))*100
        high_limit=1040 if kind=="pressure" else {700:3500,850:1850,925:1100}[kind]
        steep_limit=6 if kind=="pressure" else 80
        return bool(level>=high_limit or np.percentile(gradients[kind][rows,cols],90)>=steep_limit)
    return suspicious
