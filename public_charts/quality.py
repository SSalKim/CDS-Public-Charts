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


def terrain_weighted_pressure(pressure, terrain, latitude, longitude):
    weight = np.clip((terrain - TERRAIN_SMOOTH_LOW) / (TERRAIN_SMOOTH_HIGH - TERRAIN_SMOOTH_LOW), 0, 1)
    if not np.any(weight):
        return pressure
    dy = float(np.median(np.abs(np.diff(latitude)))) * 111
    dx = float(np.median(np.abs(np.diff(longitude)))) * 111 * np.cos(np.deg2rad(35))
    finite = np.isfinite(pressure)
    filled = np.where(finite, pressure, np.nanmean(pressure))
    smooth = gaussian_filter(filled, (TERRAIN_SMOOTH_KM / dy, TERRAIN_SMOOTH_KM / dx), mode="nearest")
    return np.where(finite, (1 - weight) * pressure + weight * smooth, pressure)


def display_fields(arrays, cycle, domain="asia"):
    """Fields as drawn: from the KMA-style cutover, Asia MSLP is terrain-smoothed for every model."""
    if (domain != "asia" or not policy.kma_style(cycle) or "pressure_msl" not in arrays or "terrain_height" not in arrays
            or np.shape(arrays["pressure_msl"]) != np.shape(arrays["terrain_height"])):
        return arrays
    smoothed = terrain_weighted_pressure(arrays["pressure_msl"], arrays["terrain_height"],
                                         arrays["latitude"], arrays["longitude"])
    return arrays if smoothed is arrays["pressure_msl"] else {**arrays, "pressure_msl": smoothed}


def terrain_masks(arrays):
    # Preserve the old call contract; no altitude or below-ground field is hidden.
    return {}


def mask_statistics(masks):
    return {str(key): {"masked_cells": int(np.count_nonzero(~value)), "total_cells": int(value.size)}
            for key,value in masks.items()}


def closed_contour_predicate(arrays):
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
