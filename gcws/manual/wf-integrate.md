# Integrate and correct peaks

## Automatic integration

A run is integrated when it is loaded. Each signal has its own integration method: one for the FID and one for
the MS traces (TIC, BPC, EIC).

- **Integrate** (`F5`) integrates the active chromatogram again with its method.
- **Integration > Integrate all** (`Shift+F5`) does it for all loaded chromatograms.

By default the integrator sets its main parameters itself from the signal: it measures the noise and the
typical peak width and derives the smoothing and the thresholds from them. That is why a new kind of sample
usually integrates sensibly without any setting. The values it chose are shown in the **Properties** panel
under **Parameters used**.

## Change the method

Open **Integration > Integration method panel**.

1. In the **Parameters** tab, untick **Auto** beside a value to set it yourself. The parameters are explained
   in [Settings and defaults](ref-settings.md#integration-method).
2. **Apply to active** integrates the active chromatogram with the new values; **Apply to all** uses them for
   all loaded chromatograms.
3. **Save as...** stores the method under a name. The box at the top lists the stored methods.
4. **Auto parameters** sets width, slope, threshold and smoothing back to automatic.

The **Timed events** tab changes a parameter from a given time on, or switches the integrator off for a
range - for example **Integrator off** at 0 min and **Integrator on** at 5 min. **Add event** adds a row;
choose the time, the event and its value.

## Correct single peaks by hand

The **Integration tools** toolbar holds the mouse tools. Each has a key. They work in both chromatograms, on
the signal of the panel you use them in.

| Tool | Key | How to use it |
|---|---|---|
| **Select / zoom** | `Z` | Click a peak to select it; drag a box to zoom. Drag the start or end of the selected peak to move it. |
| **Pan** | `H` | Drag to move the view. |
| **Draw baseline** | `B` | Drag from where the baseline should start to where it should end. With `Shift` the line ends exactly at the mouse height. |
| **Split (drop line)** | `S` | Click where a peak should be split by a vertical line. The line snaps to the valley. |
| **Delete peak** | `D` | Click a peak, or drag across several. |
| **Add peak** | `A` | Drag from the start to the end of a peak that was not found. |
| **Move start/end** | `M` | Drag the start or the end of a peak. |
| **Merge peaks** | `G` | Drag across the peaks that should be one. |
| **Tangent skim** | `K` | Click the large peak, then the small peak riding on its tail. With `Shift` the skim line is an exponential curve instead of a straight line. |
| **Negative peak** | `N` | Drag across a peak that points downwards. |
| **Reset range** | `R` | Drag across a range to discard all manual changes in it. |

With the other tools, holding `Shift` switches off the snapping to valleys and data points.

After use the tool stays active if **Keep an integration tool active after use** is ticked in
**Edit > Preferences...**; otherwise the program returns to **Select / zoom**.

## What happens to manual changes

- Every manual change is stored as a **manual event** and is **applied again after every re-integration**.
  Changing the method does not lose your corrections.
- The **Manual events** tab of the **Integration method** panel lists them with user, time and comment.
  Untick one to switch it off, **Delete selected** removes it, **Remove all manual changes** starts over.
- Each change is one undo step (`Ctrl+Z`) and is written to the audit trail.

## Delete peaks in the table

Mark rows in **Peaks / substances** (with `Ctrl` or `Shift` for several) and press `Delete`, or use
**Delete peak(s)**. All marked peaks are deleted as one undo step, and they stay deleted after a
re-integration.

## Split a peak that contains two substances

When two substances elute so closely that the chromatogram shows one peak, the mass spectra can still tell them
apart. Select the peak and choose **Identify > Deconvolution...** (`Ctrl+K`), or **Split by deconvolution...**
in the right-click menu of the peak table.

1. The window shows the peak with the curve of each component found in the MS data, and each component's share
   of the peak area.
2. Tick the components that are real. Weak ones (signal-to-noise below 20, or less than 1 % of the peak) are
   listed but not ticked.
3. **Split peak** replaces the peak by one peak per ticked component. Each keeps the spectrum of its component
   for the library search.

The areas come from fitting the component curves to the chromatogram itself - for an FID peak to the FID
signal. If that fit is not good enough, the proportions of the MS components are used instead, and the status
line says so. The split can be undone.

**Visible range** and **Whole run** in the same window list the components of a stretch of the chromatogram or
of the whole run. **Add as peaks** integrates ticked components as new peaks, **Use for peak spectrum** gives
the selected peak the spectrum of a component, and **Library hits...** searches a component.
