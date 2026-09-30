# Load and view data

## Load runs

1. In the **Folders** panel, go to the folder of the batch. **Choose the root folder...** sets where the tree
   starts, **↑** goes one folder up, and **File > Open folder...** (`Ctrl+Shift+O`) shows a folder directly.
2. Double-click a run (an Agilent `.D` folder or a Shimadzu `.qgd` file), or mark several and press **Load**.
   A right-click offers **Load as** with a role (for example as Blank) and **Load all runs in folder**.
3. The runs appear in the **Loaded samples** list below the tree, in injection order and each in its own
   colour. Click one to make it the active chromatogram.

**File > Load chromatograms...** (`Ctrl+L`) asks for a folder instead of using the tree: a single run, or a
batch folder, whose runs are then all loaded. **File > Load Shimadzu QGD files...** asks for `.qgd` files.

GC Workspace reads the raw files directly: Agilent ChemStation (`*.ch`, `data.ms`), Agilent MassHunter
(`AcqData`) and Shimadzu full-scan `.qgd`. No ChemStation or MassHunter software is needed, and the raw
files are never changed.

## The Loaded samples list

A right-click on a loaded sample gives:

| Item | What it does |
|---|---|
| **Role** | Sample, Blank, Blank + ISTD, Standard or Alkane ladder. |
| **Assign blanks...** | Which blank runs belong to this sample. |
| **Double determination with** | Opens the double determination with the chosen partner (the likely partner is suggested first). |
| **Double determination / replicates...** | Opens the **Replicates / results** panel. |
| **Signal in Chromatogram 1** | FID, TIC, BPC or an extracted ion for this run. |
| **Extracted ion (EIC)...** | Asks for the m/z values and shows their chromatogram. |
| **Show in overlay** | Whether this run is drawn behind the active one. |
| **Colour...** | The colour of this run in all plots. |
| **Rename sample...** | Another label for this run (the files keep their names). |
| **Show in folder tree** | Selects the run in the **Folders** panel. |
| **Close** / **Close others** | Removes runs from the project (nothing is deleted on disk). |

Drag entries to change their order.

## The two chromatograms

**Chromatogram 1** and **Chromatogram 2** show the same runs on one common time axis. The MS signal is shifted
by each run's FID-MS delay, so a substance is at the same position in both.

In each panel you choose:

| Control | What it does |
|---|---|
| Signal box | **FID**, **TIC** (total ion current), **BPC** (base peak chromatogram) or an **EIC**. |
| **subtract blank** | Shows and integrates the signal minus the assigned blank. See [Blanks, internal standards and quantification](wf-quantify.md). |
| **Solvent cut** | Leaves out everything before the solvent end time. |
| **Overlay** | Shows the other loaded runs behind the active one. |
| **Normalize** | Scales every trace to its own maximum, to compare shapes. |
| **Stack** | Moves the traces apart vertically. |
| Labels box | What is written at the peaks: retention time, peak number, name, or nothing. |
| **Export...** | Saves the chromatogram as a picture. |
| **⋯** | Holds the controls that do not fit when the panel is narrow. |

## Zoom and move

These work in both chromatograms together, whichever tool is chosen unless noted:

| Gesture | Result |
|---|---|
| Drag a box with the left button (**Select / zoom** tool) | Zooms into the box, time and intensity. |
| Double-click in the plot | The whole run, both intensities fitted. |
| Wheel over the plot | Zooms the time around the mouse. |
| Drag on the time axis | Moves left and right. |
| Drag on the intensity axis | Moves up and down. |
| Right-drag or wheel on the intensity axis | Makes the peaks taller or flatter; the baseline stays at the bottom. |
| Double-click on the intensity axis | Fits the intensity, keeps the time window. |
| Click a row in **Peaks / substances** | Zooms both chromatograms to that peak. |

An intensity you set by hand stays when you zoom the time afterwards. A double-click fits it again.

## Look at a spectrum

| Gesture in a chromatogram | Result in the **Mass spectrum** panel |
|---|---|
| Click a peak | The spectrum of that peak. |
| Right-click anywhere | The spectrum at that time. |
| Right-drag over a range | The mean spectrum of the range. |
| `Shift` + right-drag | Sets a background range that is subtracted from the spectra you look at. |

In the **Mass spectrum** panel, `←` and `→` step one scan, and `Esc` returns to the selected peak. Clicking an
ion in the spectrum shows that ion's chromatogram (EIC) in the MS chromatogram.

**Subtract baseline** in the main toolbar gives a spectrum minus one chosen baseline scan: click the button,
right-click the apex of the peak, then right-click a point on the baseline. `Esc` or the button again ends it.

## Extracted ion chromatograms

**View > Extracted ion chromatogram...** (`Ctrl+I`) asks for one or more m/z values and shows their trace as
the signal of the MS chromatogram. Choose **TIC** in the signal box of that panel to return.

## Solvent cut

The solvent peak at the start of a run disturbs scaling, integration and deconvolution. The cut leaves it out.

- Tick **Solvent cut** in a chromatogram panel, or **Chromatogramm > FID solvent cut** /
  **TIC/MS solvent cut**.
- Set the end time in the **Chromatogramm** menu (**Solvent end RT**) or with **Integration > Solvent cut...**.
  FID and MS have separate times; the FID time is the same value as the NIAS parameter *Solvent end*.

Everything before that time is left out of the plots, the integration, whole-run deconvolution, the library
search of the peaks and the exports. The change can be undone and is saved with projects and methods.

## Save a picture

**File > Export chromatogram...** or **Export...** in a chromatogram panel: one chromatogram or both stacked,
as PNG, JPEG, TIFF, BMP, SVG or PDF, with size, resolution and an optional title line. **Copy to clipboard**
puts it straight into another program.
