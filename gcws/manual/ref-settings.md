# Settings and defaults

Every setting that changes a result, with its default - the value it has before anyone changes it - and a hint
when to change it. The defaults are read from the program itself.

Settings are saved with the project, and as a whole by
**Method > Save current settings as Method...** (see [Methods and projects](wf-methods.md)).

## Integration method

**Integration method** panel, **Parameters** tab. There is one method for the FID and one for the MS traces.
*Automatic* means the program derives the value from the signal; the value it used is shown in the
**Properties** panel.

| Setting | Default | What it does | Change it when |
|---|---|---|---|
| **Peak width (FWHM)** | {{IntegrationMethod.peak_width}} | The width of a typical peak at half height. It sets how strongly the signal is smoothed and how narrow a peak may be. | very narrow or very broad peaks are missed. |
| **Slope sensitivity** | {{IntegrationMethod.slope_sensitivity}} | How steeply the signal must rise, as a multiple of the noise, for a peak to start. | noise is integrated (higher), or flat peaks are missed (lower). |
| **Threshold (height)** | {{IntegrationMethod.threshold}} | The smallest height above the baseline that counts as a peak. | small peaks are missed (lower) or noise is integrated (higher). |
| **Smoothing window** | {{IntegrationMethod.smoothing_window}} | Number of data points the signal is smoothed over before peaks are looked for. | - |
| **Min. valley depth** | {{IntegrationMethod.min_valley_depth}} | Two peaks are only separated when the valley between them is at least this deep. | one peak is split at a small dip (higher), or two peaks stay merged (lower). |
| **Area reject** | {{IntegrationMethod.area_reject}} | Peaks with a smaller area are dropped. 0 = none. | you want a shorter peak list. |
| **Height reject** | {{IntegrationMethod.height_reject}} | Peaks with a smaller height are dropped. 0 = none. | as above. |
| **Min. S/N** | {{IntegrationMethod.min_sn}} | Peaks with a smaller signal-to-noise ratio are dropped. 0 = none. | as above. |
| **Baseline** | {{IntegrationMethod.baseline_mode}} | **drop**: a group of overlapping peaks gets one common baseline and is divided by vertical drop lines. **valley**: the baseline goes through every valley. | peaks sit on a rising hump and should each have their own baseline (valley). |
| **Skim mode** | {{IntegrationMethod.skim_mode}} | How a small peak riding on the tail of a large one is separated: **none** (drop line), **tangent** (straight line under the rider), **exponential** (a curve following the tail), **auto** (the program chooses). | riders on solvent or main peaks are over- or underestimated. |
| **Tail skim height ratio** | {{IntegrationMethod.tail_skim_ratio}} | A rider on the tail is skimmed when the large peak is at least this many times higher. | - |
| **Front skim height ratio** | {{IntegrationMethod.front_skim_ratio}} | The same for a rider on the front. | - |
| **Skim valley ratio** | {{IntegrationMethod.skim_valley_ratio}} | A rider is only skimmed when it is no more than this many times higher than the valley before it - that is, when it really sits on the other peak. | - |
| **Shoulders** | {{IntegrationMethod.shoulders}} | Whether shoulders - peaks without a valley of their own - are separated: **off**, **drop** (by a drop line) or **tangent**. | a shoulder is a substance of its own. |
| **Detect negative peaks** | {{IntegrationMethod.negative_peaks}} | Also integrates peaks that point downwards. | - |
| **Baseline tracking (end peaks only at the baseline)** | {{IntegrationMethod.baseline_tracking}} | A peak may only end where the signal is back near the baseline. | peaks end too early on a drifting baseline. |
| **Area factor** | {{IntegrationMethod.area_unit_factor}} | Reported area = integral (signal × seconds) × factor. 10 gives the area units of ChemStation for the FID. | the areas must match another system. |

### Automatic deconvolution split

The group **Automatic deconvolution split** on the same tab. See
[Co-eluting peaks: the automatic deconvolution split](wf-integrate.md#co-eluting-peaks-the-automatic-deconvolution-split).
The deconvolution itself (which components the MS shows) uses the settings of
[Deconvolution](ref-settings.md#deconvolution).

| Setting | Default | What it does | Change it when |
|---|---|---|---|
| **Deconvolution split** | {{IntegrationMethod.deconv_split}} | **Automatic**: after the integration, every peak that holds several deconvoluted MS components is split into one peak per component. The areas come from fitting the components to this trace. **Off**: peaks are only split by hand. | co-eluting substances make mixed spectra, unknowns and too large areas. |
| **Min. component share** | {{IntegrationMethod.deconv_min_share %}} | A component with a smaller share of the fitted signal is not split off; its signal stays with its neighbours. | small impurities are split off that you do not want as peaks (higher). |
| **Min. component S/N** | {{IntegrationMethod.deconv_min_sn}} | A component with a smaller signal-to-noise ratio in the MS is not split off. | as above. |
| **Min. fit R²** | {{IntegrationMethod.deconv_fit_r2}} | Below this fit quality the areas are divided in the proportions of the MS components instead of by the fit. Such splits are listed by the Report² rule **Automatic deconvolution split**. | - |
| **Min. shape correlation** | {{IntegrationMethod.deconv_min_r}} | A component is only split off when its fitted curve follows the trace (Pearson r, as mzmine's GC spectral deconvolution). | components are split off that the FID does not show (higher). |
| **Excluded model m/z** | {{IntegrationMethod.deconv_exclude_mz}} | Components whose model ion is one of these masses (column bleed) are never split off. | another background ion produces components. |

## Timed events

**Integration method** panel, **Timed events** tab. An event takes effect from its time on.

| Event | Value | What it does |
|---|---|---|
| **Integrator off** / **Integrator on** | | No peaks are integrated between the two. |
| **Slope sensitivity**, **Threshold**, **Peak width**, **Area reject**, **Height reject**, **Min. S/N** | a number | Changes that parameter from this time on. |
| **Shoulders** | off / drop / tangent | Changes the shoulder treatment. |
| **Skim mode** | none / tangent / exponential / auto | Changes the skim mode. |
| **Tail skim height ratio**, **Front skim height ratio**, **Skim valley ratio** | a number | Change the skim ratios. |
| **Baseline now** | | Puts a baseline point on the signal at this time: a group of peaks is divided there. |
| **Baseline next valley** | | Puts a baseline point at the next valley after this time. |
| **Baseline hold on** / **Baseline hold off** | | Keeps the baseline horizontal between the two, at the height of the signal where the hold began. |
| **Baseline at valleys on** / **Baseline at valleys off** | | Puts the baseline through every valley between the two. |
| **Baseline backward** | | The next group of peaks gets a horizontal baseline drawn backwards from its end. |
| **Split peak** | | Splits the peak at this time with a drop line. |
| **Area sum on** / **Area sum off** | | Sums everything between the two into one peak. |
| **Negative peaks on** / **Negative peaks off** | | Integrates negative peaks between the two. |
| **Solvent peak on** / **Solvent peak off** | | Marks the peaks between the two as solvent peaks (type S). |
| **Deconvolution split off** / **Deconvolution split on** | | No automatic deconvolution split between the two (for example over an oligomer hump). |

## Solvent cut

| Setting | Default | What it does |
|---|---|---|
| **FID solvent cut** | off | Leaves out the FID signal before the solvent end. The last choice is remembered for the next start. |
| **Solvent end RT** (FID) | 5.5 min | In FID minutes. The same value as the NIAS parameter *Solvent end*. |
| **TIC/MS solvent cut**, **Solvent end RT** (MS) | off | The same for the MS traces, in MS minutes. Until you edit it, the MS time follows the FID time minus each run's FID-MS delay. |

## Blank subtraction

**Quantify > Blank subtraction settings...** The window has two groups:
**Chromatogram ("FID − Blank", "TIC − Blank")** for the subtraction of whole traces, and
**Peak list ("In blank" column, hiding blank peaks)** for the blank check of single peaks.

| Setting | Default | What it does | Change it when |
|---|---|---|---|
| **Automatic blank subtraction** | {{BlankOptions.auto}} | Rebuilds the subtracted traces after every change of these settings or of an integration. Off: only with **Subtract**. | the rebuilding slows you down with many runs. |
| **Subtract** | {{BlankOptions.source}} | Which blank is subtracted: **Blank**, **Blank + ISTD**, or **Blank and Blank + ISTD (averaged)**. | - |
| **FID traces** | {{BlankOptions.mode_fid}} | **Blank peaks only**: the sample keeps its own baseline. **Whole blank trace**: the blank's baseline is subtracted too. | - |
| **MS traces** | {{BlankOptions.mode_ms}} | As above. The whole trace also removes the rise of the column bleed. | - |
| **Align the blank in time (cross-correlation)** | {{BlankOptions.align}} | Shifts the blank so that its peaks lie on the sample's before subtracting. | - |
| **Alignment search range** | {{BlankOptions.max_shift}} min | The largest shift that is tried. | the blank was run much later. |
| **Blank factor** | {{BlankOptions.scale}} × | The blank is multiplied by this before it is subtracted. | the blank was injected with a different amount. |
| **Blank baseline window** | {{BlankOptions.env_window}} min | For **Blank peaks only**: how wide the stretch is over which the blank's own baseline is found. | broad blank humps are treated as baseline (larger). |
| **Never below the sample's own baseline** | {{BlankOptions.clip}} | Prevents dips where the blank is larger than the sample. | - |
| **Blank level when sample <** | {{BlankOptions.ratio_limit}} × | A peak is *at blank level* when its area is below this many times the blank peak's area. Used by the **In blank** column and **Hide blank peaks**. | - |
| **MS: spectra must agree (cosine ≥)** | {{BlankOptions.spectral_min}} | With MS data, a blank peak only matches a sample peak when their spectra are this similar. | - |
| **RT tolerance** | {{BlankOptions.rt_tol}} | How far apart a sample peak and a blank peak may be. Automatic: the NIAS blank tolerance for the FID, 0.03 min for MS. | - |

Internal standards are never subtracted and never marked as blank. The NIAS mg/kg calculation keeps its own
blank correction and is not affected by these settings.

## Deconvolution

**Settings** in the **Deconvolution** window. The three **Presets** set the advanced values together.

| Setting | Default | What it does |
|---|---|---|
| **Presets: Resolution** | medium | How close two components may be in time and still be told apart. **high** separates closer ones (apex tolerance 0.3 scans), **low** merges more (1.0). |
| **Presets: Sensitivity** | medium | How weak an ion may be and still count. **high**: noise factor 2; **low**: noise factor 5 and at least 5 ions. |
| **Presets: Shape** | medium | How alike the profiles of the ions of one component must be. **strict** 0.95, **medium** 0.90, **loose** 0.85. |
| **Window ±** | {{DeconvSettings.window}} min | The stretch around the peak that is looked at. |
| **Noise factor** | {{DeconvSettings.noise_factor}} σ | An ion must rise this many times above its noise. |
| **Min. profile r** | {{DeconvSettings.shape_r}} | The least correlation between the profile of an ion and that of its component. |
| **Apex tolerance** | {{DeconvSettings.apex_tol}} scans | Ions whose maxima are no further apart than this belong to one component. |
| **Min. ions** | {{DeconvSettings.min_ions}} | A component needs at least this many ions. |

**Save as default** makes the settings apply to the spectrum mode **Deconvoluted component** and to the
library search as well.

## Library search

The settings of a search method are explained in
[How the library search works](library-search.md#the-settings-that-matter).

The choices of the search start window are remembered:

| Choice | Default |
|---|---|
| **Peaks** | TIC peaks |
| **Give the names also to the FID peaks at the same time** | on |
| **Spectrum** | Assigned component, otherwise average minus background |
| **Skip peaks that already have a name** | off |
| **Only peaks with a score below** | off; 80 |

## Double determination

All settings with their defaults are in
[How the double determination works](double-determination.md#settings).

**Accept a difference up to** (the difference limit, 30 % unless changed) is a NIAS report parameter and is
set at the top of the **Double determination** tab.

## Quantification

The **NIAS parameters** table of the **Quantification** panel holds the numbers of the NIAS calculation and of
the reports. Each row has its unit beside it. The ones that matter most in daily work:

| Parameter | Unit | What it does |
|---|---|---|
| **Reporting limit** | mg/kg | Substances below it are not reported. |
| **Solvent end** | min | Peaks before it are not evaluated. The same value as the FID solvent cut. |
| **Quality threshold** | | The library score from which a name counts as *Accepted* (70 unless changed). This manual calls it the quality limit. |
| **Duplicate RT tolerance** | min | Classic pairing: how far apart the two determinations' peaks may be. |
| **Duplicate difference limit** | % | The largest accepted difference between two determinations. |
| **Cell area**, **Coverage**, **O/V ratio** | | The migration cell; also set by **Migration conditions...**. |
| **Internal standard amount**, **FC17 concentration**, **BBP-d4 concentration**, **DnNP-d4 concentration** | mg/mL | The standards of the classic NIAS calculation. |
| **Blank RT tolerance** | min | How far apart a sample peak and a blank peak may be to be the same. |
| **Extract volume** | mL | For the total extraction. |

Once the **Internal standards** table of the panel is filled in, its concentrations are used instead of the
ISTD concentrations of the parameter table.

## Preferences

See [Methods and projects](wf-methods.md#preferences).

## What the program remembers between sessions

Besides the settings above, the program remembers for the next start: the window layout and look, the root
folder of the tree, which signals the two chromatograms show and which of them is blank-subtracted, the
columns of the peak table and its value filter, the choices of the export and search windows, the current
processing method, and whether the m/z axis shows the whole scan range.
