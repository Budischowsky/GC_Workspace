# Integrate and correct peaks

## Automatic integration

A run is integrated by **Method > Run Method** (`Ctrl+R`); loading only reads it. Each signal has its own integration method: one for the FID and one for
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
| **Merge peaks** | `G` | Drag across the peaks that should be one. On deconvoluted peaks, a click or a drag undoes their deconvolution split. |
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

## Co-eluting peaks: the automatic deconvolution split

Instead of splitting peak by peak, the integration method can do it for the whole run. Set **Deconvolution
split** to **Automatic** in the group **Automatic deconvolution split** of the **Integration method** panel
(**Parameters** tab) and click **Apply to all**. The setting is part of the integration method, so it is saved
with processing methods and used by the automation.

1. The whole run is deconvoluted once (in the background; the panel says *Deconvoluting the run ...*).
2. After the integration and your manual changes, every peak that holds two or more components is split, the
   same way as **Split peak** does it: the component curves are fitted to the trace, the peak's area is divided
   by the fitted areas, and the total stays exactly the same. If the fit is below **Min. fit R²**, the MS
   component proportions are used.
   Two components with almost the same curve a scan or so apart (a deuterated standard and its slightly
   lighter isotopologue) fit the trace about equally well either way round; the curves are then placed where
   the MS signal of the peak as a whole lies, so the standard keeps its peak and its spectrum.
   A peak in which the whole-run deconvolution finds only one component, although its trace shows a shoulder
   (or one component fits it badly), gets a **closer look**: its MS is deconvoluted again, more sensitively
   and with a pass over what the found component leaves. This finds a substance that elutes on the tail of a
   larger one and shares most of its ions. It is switched by **Closer look at shoulders**.
   A component that no peak holds, but that elutes into the tail (or front) of a peak -- a small substance
   behind a large, often overloaded peak whose end the integrator set before it -- is a **hidden component**:
   the peak is extended over it, to the lowest point of the trace after it, and split. This is kept only when
   the fit to the trace splits the hidden component off; otherwise the peak keeps its integrated bounds. Such
   splits say *peak extended over the hidden component* in their comment. A hidden component needs an MS S/N
   of at least 20 at every detection level.
3. Components that are too weak, whose model ion is a bleed ion, whose curve the trace does not show, or whose
   spectrum is the same as their neighbour's are not split off. A component with less than **Min. component
   share** of the peak is split off all the same when it is resolved from the peak's main substance (its apex
   at least two half-height widths from the main apex) and its area would pass the method's **Area reject**:
   in the tail of a large peak a substance of its own has well under 1 %. A hidden component always needs
   that area.
4. Each fragment keeps its component spectrum. The library search searches the fragments with these clean
   spectra, also when it searches the TIC peaks, so every FID peak gets its own name.

The panel shows how many peaks were split; the tooltip of that line lists the peaks left unsplit and why
(for example a shoulder for which the closer look found only one component). The automatic splits are not listed among the manual events: they are
made again at every integration from the present settings.

- **Keep unsplit (no automatic deconvolution split)** in the right-click menu of the peak table integrates the
  selected peak (for a fragment: its whole peak) as one peak again. **Allow automatic deconvolution split**
  removes that mark. The mark is a manual event and can be undone.
- **Merge deconvoluted peaks** (right-click menu of the peak table, or the **Merge peaks** tool `G` in
  Chromatogram 1 or 2: click a fragment or drag across the fragments) undoes the deconvolution split, in
  the FID and in the TIC. A split you made by hand is removed; an automatic split gets the Keep unsplit
  mark. Ctrl+Z brings the split back.
- To split a peak differently, keep it unsplit first and then split it by hand with
  **Identify > Deconvolution...**. A peak you split by hand is never split automatically.
- The timed events **Deconvolution split off** / **Deconvolution split on** switch it off for a stretch.
- In a double determination a split found in one injection only is carried over to the other one when the
  components fit its trace (see [How the double determination works](double-determination.md#settings)).

**Visible range** and **Whole run** in the same window list the components of a stretch of the chromatogram or
of the whole run. **Add as peaks** integrates ticked components as new peaks, **Use for peak spectrum** gives
the selected peak the spectrum of a component, and **Library hits...** searches a component.
