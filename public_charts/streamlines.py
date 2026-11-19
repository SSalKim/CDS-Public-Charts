"""Vectorized streamlines equivalent to matplotlib's streamplot(broken_streamlines=False).

matplotlib integrates one trajectory at a time in Python (about 7 s per Asia chart). With
broken_streamlines=False a trajectory's path never depends on other trajectories: the
density mask only decides whether a seed may start. So every seed is integrated at once
with the same adaptive Heun (RK12) scheme, step limits, boundary rules and length limits,
and seeds are then accepted in matplotlib's spiral order against the same mask.
"""
import numpy as np

MAX_ERROR = 0.003


def _seed_order(ny, nx):
    """matplotlib's _gen_starting_points: start at a corner and spiral inward."""
    xfirst, yfirst, xlast, ylast = 0, 1, nx - 1, ny - 1
    x = y = 0
    direction = "right"
    order = []
    for _ in range(nx * ny):
        order.append((x, y))
        if direction == "right":
            x += 1
            if x >= xlast:
                xlast -= 1
                direction = "up"
        elif direction == "up":
            y += 1
            if y >= ylast:
                ylast -= 1
                direction = "left"
        elif direction == "left":
            x -= 1
            if x <= xfirst:
                xfirst += 1
                direction = "down"
        else:
            y -= 1
            if y <= yfirst:
                yfirst += 1
                direction = "right"
    return order


def _interp(a, xi, yi):
    ny, nx = a.shape
    x = xi.astype(int)
    y = yi.astype(int)
    xn = np.clip(x + 1, 0, nx - 1)
    yn = np.clip(y + 1, 0, ny - 1)
    xt, yt = xi - x, yi - y
    a0 = a[y, x] * (1 - xt) + a[y, xn] * xt
    a1 = a[yn, x] * (1 - xt) + a[yn, xn] * xt
    return a0 * (1 - yt) + a1 * yt


def _integrate(x0, y0, u, v, speed, maxds, maxlength, sign):
    """Integrate many trajectories in one direction; return point lists, lengths and mask steps."""
    ny, nx = u.shape
    count = len(x0)
    xi, yi = x0.astype(float).copy(), y0.astype(float).copy()
    ds = np.full(count, maxds)
    stotal = np.zeros(count)
    visited = [[] for _ in range(count)]

    def inside(x, y):
        return (x >= 0) & (x <= nx - 1) & (y >= 0) & (y <= ny - 1)

    start_inside = inside(xi, yi)
    points = [[(a, b)] if ok else [] for a, b, ok in zip(x0, y0, start_inside)]
    active = np.flatnonzero(start_inside)

    def velocity(x, y):
        s = _interp(speed, x, y)
        valid = np.isfinite(s) & (s != 0)
        with np.errstate(divide="ignore", invalid="ignore"):
            dt = 1.0 / s
            return sign * _interp(u, x, y) * dt, sign * _interp(v, x, y) * dt, valid

    while len(active):
        x, y, step = xi[active], yi[active], ds[active]
        k1x, k1y, ok1 = velocity(x, y)
        tx, ty = x + step * k1x, y + step * k1y
        probe = ok1 & inside(tx, ty)
        out = ok1 & ~probe
        safe_x, safe_y = np.where(probe, tx, x), np.where(probe, ty, y)
        k2x, k2y, ok2 = velocity(safe_x, safe_y)
        # matplotlib: an off-grid probe ends with an Euler step to the boundary.
        for index in active[out]:
            px, py = points[index][-1]
            cx, cy, okc = velocity(np.array([px]), np.array([py]))
            if not okc[0]:
                continue
            cx, cy = float(cx[0]), float(cy[0])
            dsx = np.inf if cx == 0 else (px / -cx if cx < 0 else (nx - 1 - px) / cx)
            dsy = np.inf if cy == 0 else (py / -cy if cy < 0 else (ny - 1 - py) / cy)
            boundary = min(dsx, dsy)
            points[index].append((px + cx * boundary, py + cy * boundary))
            stotal[index] += boundary
        stop = ~ok1 | out | ~ok2
        dx1, dy1 = step * k1x, step * k1y
        dx2, dy2 = step * .5 * (k1x + k2x), step * .5 * (k1y + k2y)
        error = np.hypot((dx2 - dx1) / (nx - 1), (dy2 - dy1) / (ny - 1))
        accept = ~stop & (error < MAX_ERROR)
        nx_, ny_ = x + dx2, y + dy2
        landed = accept & inside(nx_, ny_)
        stop |= accept & ~landed  # matplotlib: an accepted step leaving the grid ends the line
        over = landed & (stotal[active] + step > maxlength)
        for local in np.flatnonzero(landed):
            index = active[local]
            visited[index].append((nx_[local], ny_[local]))
            if not over[local]:
                points[index].append((nx_[local], ny_[local]))
        stotal[active[landed & ~over]] += step[landed & ~over]
        xi[active[landed]], yi[active[landed]] = nx_[landed], ny_[landed]
        stop |= over
        with np.errstate(divide="ignore", invalid="ignore"):
            resized = np.where(error == 0, maxds, np.minimum(maxds, .85 * step * np.sqrt(MAX_ERROR / error)))
        ds[active] = np.where(np.isfinite(resized), resized, maxds)
        active = active[~stop]
    return points, stotal, visited


def streamlines(x, y, u, v, *, density=1.0, minlength=.1, maxlength=4.0):
    """Return trajectories as (n, 2) arrays in data coordinates, in matplotlib's seed order."""
    x, y = np.asarray(x, float), np.asarray(y, float)
    if x.ndim == 2:  # meshgrid input, as matplotlib's Grid accepts
        x, y = x[0], y[:, 0]
    u, v = np.asarray(u, float), np.asarray(v, float)
    ny, nx = u.shape
    dx, dy = x[1] - x[0], y[1] - y[0]
    mask_ny, mask_nx = (30 * np.broadcast_to(density, 2)).astype(int)[::-1]
    gu, gv = u / dx, v / dy
    speed = np.hypot(gu / (nx - 1), gv / (ny - 1))
    maxds = min(1. / mask_nx, 1. / mask_ny, .1)
    half = maxlength / 2.
    order = _seed_order(mask_ny, mask_nx)
    # Same floating-point path as matplotlib's DomainMap, including its edge seeds that land a
    # hair outside the grid and are therefore rejected.
    to_mask_x, to_mask_y = (mask_nx - 1) / (nx - 1), (mask_ny - 1) / (ny - 1)
    to_grid_x, to_grid_y = 1. / to_mask_x, 1. / to_mask_y
    seeds = np.array([(xm * to_grid_x, ym * to_grid_y) for xm, ym in order])
    backward = _integrate(seeds[:, 0], seeds[:, 1], gu, gv, speed, maxds, half, -1)
    forward = _integrate(seeds[:, 0], seeds[:, 1], gu, gv, speed, maxds, half, 1)
    occupied = np.zeros((mask_ny, mask_nx), bool)
    result = []
    for index, (xm, ym) in enumerate(order):
        if occupied[ym, xm]:
            continue
        if backward[1][index] + forward[1][index] <= minlength:
            continue
        cells = {(ym, xm)}
        for gx, gy in backward[2][index] + forward[2][index]:
            cells.add((round(float(gy) * to_mask_y), round(float(gx) * to_mask_x)))
        for cell in cells:
            occupied[cell] = True
        path = np.array(backward[0][index][::-1] + forward[0][index][1:])
        result.append(np.column_stack((x[0] + path[:, 0] * dx, y[0] + path[:, 1] * dy)))
    return result
