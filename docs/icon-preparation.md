# ICON Staging

ICON is intentionally not in policy.RETENTION, the automatic production model
roster. The shared existing Drive, published manifest and model time ranges are
unchanged. The second Drive uses PUBLIC_CHARTS_DRIVE2_* secrets and can hold ICON
and future models; it is not reserved exclusively for ICON.

## Inputs and Layout

- DWD ICON (R03B07 L120), native unstructured global grid, 2,949,120 cells.
- 00/12Z: 0-180h; 06/18Z: 0-120h. Display: 3h to84h, 90/96h, then12h.
- New v1 parameter/level/run paths first; exact-cycle legacy bz2 paths second.
- Native minute/hour forecast units are normalized through ecCodes.
- Reuse pyresample's four-neighbour inverse-distance weights on a 0.125-degree
  chart source grid. This is point interpolation, not an area-conservative
  precipitation remap; no one-degree extraction is used.
- Validate cycle, forecast step, pressure level, grid UUID, units, missing data
  and Earth-relative wind flags. Coordinates must match each GRIB's native UUID.
- Derive pressure-level specific humidity from native RELHUM and temperature
  using MetPy's liquid-water reference, matching DWD's RELHUM definition.
- Use native accumulated SNOW_GSP + SNOW_CON for snow water equivalent, not an
  endpoint rain/snow categorical approximation. Fresh snow depth remains the
  existing SR diagnostic, not a prediction of settled ground snow depth.
- Reuse all current contours, map extents, winter composite dimensions, legends,
  +000h empty accumulations and lossless WebP encoding.

## Local Test

Install requirements-icon-charts.txt in an isolated runtime; the live producer's
requirements-public-charts.txt is not changed. On the existing Windows local
runtime pyresample is already installed.

```powershell
$env:PUBLIC_CHARTS_MAP_DIR = "E:\제작자료\CDS3\CDS_Git\local_tools\weathermesh\maps"
& "E:\제작자료\CDS3\CDS_Git\local_tools\weathermesh\.venv\Scripts\python.exe" `
  public_charts_icon_preview.py --cycle 2026111612 --hours 0 `
  --workdir "E:\제작자료\CDS3\CDS_Git\outputs\icon-preview-2026111612"
```

This command does not authenticate to Drive, upload/delete files, publish a Git
manifest or choose a different cycle. It retains validated local raw data and
remapping weights for repeated preview runs. Its local manifest is explicitly
marked preview_only and published=false.

After account preparation: verify the new account, test nonzero-lead rainfall
and native snowfall, measure CPU/RAM/download/upload time, then connect isolated
account storage, publication and scheduling. Do not simply append ICON to the
current shared-account automatic roster.

Sources:
- https://opendata.dwd.de/weather/nwp/v1/m/icon/p/
- https://opendata.dwd.de/weather/nwp/icon/grib/
- https://pyresample.readthedocs.io/en/latest/howtos/swath.html
- https://rcccm.dwd.de/SharedDocs/downloads/DE/modelldokumentationen/nwv/icon_d2/icon_d2_dbbeschr_aktuell.pdf?nn=16102&view=nasPublication

## Initial Verification

- Unit/regression suite: 200 tests passed, including native wind-template checks
  and isolated primary/secondary Drive authorization namespaces.
- Actual 2026111612 +000h: 20 logical outputs, 19 physical lossless WebPs, 9,774,086
  bytes. Winter composites and empty zero-hour acrain were generated and inspected.
- Actual +021/+024h precipitation/snowfall: six native files, 5,954,030 downloaded
  bytes. Both three-hour differences were finite and nonnegative on the target grid.
  Total precipitation maxima: 221.864mm (0-24h), 59.293mm (21-24h); snowfall WE:
  67.861kg/m2 (0-24h), 10.913kg/m2 (21-24h). These are source-domain maxima, not Korea-only.
- This Windows local test is not a laptop/GitHub SLA benchmark. Its cold/partially
  cached run took 415.9s, including 260.6s plotting and 5.8s encoding. No Drive
  upload or source-first-arrival-to-CDS latency was tested.
- Google authorization instructions: https://github.com/SSalKim/CDS/blob/main/docs/icon-drive-setup.md
