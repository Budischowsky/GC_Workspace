# Toolbars, tools, keys and mouse

## Main toolbar

| Button | Key | What it does |
|---|---|---|
| **Load chromatograms...** | `Ctrl+L` | Loads a run or all runs of a batch folder. |
| **Open project...** | `Ctrl+O` | Opens a saved project. |
| **Save project** | `Ctrl+S` | Saves the project. |
| **Undo** | `Ctrl+Z` | Takes back the last change. Rest the mouse on it to see which. |
| **Redo** | `Ctrl+Y` | Makes it again. |
| **Integrate** | `F5` | Integrates the active chromatogram again. |
| **Library search...** | `Ctrl+F` | Searches all integrated peaks in your libraries. |
| **Subtract baseline** | | A spectrum minus one baseline scan: click the button, right-click the apex, then right-click the baseline in a chromatogram. `Esc` or another click on the button ends it. The difference spectrum is what spectrum searches and the MSP export then use. |

## Integration tools

One tool is active at a time. It decides what the left mouse button does in a chromatogram. The status bar
shows it (*Tool: ...*).

| Tool | Key | What it does |
|---|---|---|
| **Select / zoom** | `Z` | A click selects a peak. Dragging a box zooms. Dragging a boundary of the selected peak moves it. |
| **Pan** | `H` | Dragging moves the view. |
| **Draw baseline** | `B` | Drag from the start to the end of the baseline. `Shift`: the line ends at the exact mouse height. |
| **Split (drop line)** | `S` | A click splits the peak with a vertical line, which snaps to the valley. |
| **Delete peak** | `D` | A click deletes a peak; dragging deletes all peaks crossed. |
| **Add peak** | `A` | Drag from the start to the end of the new peak. |
| **Move start/end** | `M` | Drag a start or end of a peak. |
| **Merge peaks** | `G` | Drag across the peaks to make them one. |
| **Tangent skim** | `K` | Click the parent peak, then the rider on its tail. `Shift`: exponential skim. |
| **Negative peak** | `N` | Drag across a negative peak to integrate it. |
| **Reset range** | `R` | Drag across a range to discard the manual changes in it. |

Each use of a tool other than **Select / zoom** and **Pan** makes a manual event: undoable, written to the
audit trail, and applied again after every re-integration. See
[Integrate and correct peaks](wf-integrate.md#correct-single-peaks-by-hand).

## Status bar

From left to right:

| Part | What it shows |
|---|---|
| Message | What the program just did, or the progress text of a running task. |
| Role chip | The role of the active chromatogram. |
| Blank chip | The blanks of the active sample. Yellow: **Blank: none**. |
| Delay chip | **FID−MS** and the delay in minutes. Yellow when the estimate is uncertain. |
| **Tool:** | The active integration tool. |
| Method | The processing method loaded or saved last. |
| Progress bar and **Cancel** | Shown during long tasks (library search, reports). |

## Keys

| Key | Function |
|---|---|
| `F1` | User manual |
| `F5` / `Shift+F5` | Integrate the active chromatogram / all |
| `Ctrl+F` | Library search |
| `Ctrl+E` | Library hit list of the selected peak |
| `Ctrl+N` | Search the selected peak in NIST |
| `Ctrl+Shift+E` | Investigate the selected peak in SpectrAtlas |
| `Ctrl+K` | Deconvolution |
| `Ctrl+I` | Extracted ion chromatogram |
| `Ctrl+Z` / `Ctrl+Y` | Undo / Redo |
| `Ctrl+O` / `Ctrl+S` / `Ctrl+Shift+S` | Open / save / save as project |
| `Ctrl+L` / `Ctrl+Shift+O` | Load chromatograms / open folder |
| `Ctrl+Shift+D` | Next theme |
| `Ctrl+1` ... `Ctrl+9` | Go to a panel: 1 Chromatogram 1, 2 Chromatogram 2, 3 Peaks / substances, 4 Mass spectrum, 5 Folders, 6 Quantification, 7 Replicates / results, 8 Report², 9 Integration method |
| `Ctrl+Tab` / `Ctrl+Shift+Tab` | Switch panels: a quick press goes back to the panel used before; hold `Ctrl` to see the list, `Tab` steps, releasing `Ctrl` goes there, `Esc` cancels |
| `Ctrl+Q` | Exit |
| `Z` `H` `B` `S` `D` `A` `M` `G` `K` `N` `R` | The integration tools |
| `Delete` (in the peak table) | Delete the marked peaks |
| `Ctrl+C` (in the peak table) | Copy the marked rows |
| `←` / `→` (in the spectrum) | One scan back / forward |
| `Esc` | Back to the spectrum of the selected peak; ends **Subtract baseline** |
| `F3` (double determination) | Next red row |

## Mouse in a chromatogram

| Gesture | Result |
|---|---|
| Click | Selects the peak there (in the panel the peak table does not list: the table's peak at that time). |
| Drag a box (**Select / zoom**) | Zooms into the box. |
| Double-click | The whole run in both chromatograms. |
| Wheel | Zooms the time around the mouse. |
| Drag on the time axis | Moves the time window. |
| Drag on the intensity axis | Moves the traces up and down. |
| Right-drag or wheel on the intensity axis | Scales the intensity of both panels; the bottom stays. |
| Double-click on the intensity axis | Fits both intensities, keeps the time window. |
| Right-click | The mass spectrum at that time. |
| Right-drag | The mean spectrum over the range. |
| `Shift` + right-drag | A background range, subtracted from the spectra you look at. |

## Mouse in the spectrum

| Gesture | Result |
|---|---|
| Click an ion | Shows the extracted ion chromatogram of that m/z in the MS chromatogram panel. |
| Drag a box, or wheel | Zooms the m/z range; the abundance axis fits the tallest ion in view. |
| Double-click | The whole spectrum again. |
| Right-click | The **Mass Spectrum** menu. |

## Panel title buttons

Most panels carry their title and buttons in a bar on top. Chromatogram 1, Chromatogram 2 and Mass spectrum
carry them in a narrow strip along the right edge, so the plots keep their height. When panels share a group
of tabs, the name is on the tab and the bar above the panel is slim, with the buttons only.

Right-click the title bar of a panel, its strip on the right edge, or its tab:

| Item | What it does |
|---|---|
| **Maximize** / **Restore the layout** | The same as the maximize button. |
| **Detach** / **Dock back** | Makes the panel a window of its own, or puts it back into the main window. |
| **Detach to screen 2** (3, ...) | Only with more than one screen: detaches the panel straight onto that screen, centred and sized to fit it. |
| **Move to** ▸ **Left side**, **Right side**, **Top**, **Bottom** | Docks the panel along that whole side of the window. |
| **Collapse to the side** | Hides the panel behind a button with its name on a narrow strip along its side of the window; click the button to bring the panel back. The View menu, `Ctrl+1` ... `Ctrl+9` and a layout preset bring it back as well. Collapsed panels are remembered with the layout. |
| **Close other tabs** | Only for a panel in a group of tabs: hides the other panels of the group. |
| **Close** | Hides the panel. |

When a menu command brings a closed panel, or one behind another tab, to the front, its title lights up for a
moment.

| Button | What it does |
|---|---|
| Maximize | Lets the panel fill the window; again to restore. The same as a double-click on the title. |
| Detach | Makes the panel a window of its own, for example on a second screen; again to dock it back. |
| Close | Hides the panel. The **View** menu shows it again. |
| **◀** (Folders only) | Collapses the panel to a narrow strip. |
| **⋯** (chromatograms and spectrum) | More plot controls: the ones that do not fit in a narrow panel. |
