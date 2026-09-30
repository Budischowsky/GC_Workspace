# Methods and projects

Two things store your work, and they are easy to mix up.

| | A **project** (`.gcws`) | A **processing method** |
|---|---|---|
| Holds | one piece of work: these runs, with everything done to them | the settings, to use them again on other runs |
| Contains the runs | yes (as paths to the raw data) | no |
| Contains manual integration, names, ISTD bindings, blank assignments | yes | no - these belong to single runs |
| Contains integration parameters, search method, quantification settings | yes | yes |
| Use it to | continue tomorrow, or to document what was reported | process the next batch the same way, and for the automation |

## Projects

| Function | What it does |
|---|---|
| **File > Save project** (`Ctrl+S`) | Saves. The first time it asks for a file name. |
| **File > Save project as...** (`Ctrl+Shift+S`) | Saves under a new name. |
| **File > Open project...** (`Ctrl+O`) | Opens a project. The runs are loaded and integrated again. If an integration now gives a different result than when it was saved - after an update of the program - you are told. |
| **File > Recent projects** | The projects opened last. |
| **File > Recover autosave...** | Opens the automatic save, which is written every two minutes. |
| **File > Close all chromatograms** | Closes all runs. Nothing is deleted on disk. |

A project stores the paths of the runs relative to itself, so a project saved beside the batch folder still
opens when both are moved together.

## Processing methods

**Method > Save current settings as Method...** stores, under a name and with a comment:

- the integration methods of FID and MS;
- the quantification: mode, unit, the internal standards table with learned reference spectra, the NIAS
  parameters, the solvent cut;
- blank subtraction, deconvolution and retention index settings;
- the double determination settings - every one of them;
- the migration conditions;
- the library search method and the choices of the search dialog (TIC or FID peaks, spectrum, skip named);
- the own-library search options and the report options;
- the columns of the peak table and its value filter.

**Method > Load Method...** lists the saved methods with a summary of each. Tick under **Apply** which parts
to take over and press **Load**. Loading is one undo step. The integration methods also become the default
for runs loaded afterwards.

**Import...** and **Export...** in that window move a method to a colleague's PC as a `.json` file;
**Delete** removes one.

The status bar shows the name of the method loaded or saved last.

## Integration methods and search methods

These two smaller kinds of method are parts of a processing method and can also be stored on their own:

- an **integration method** (the parameters and timed events of one signal) with **Save as...** in the
  **Integration method** panel;
- a **search method** (libraries, order, algorithm) under **Identify > Search methods...**.

## Preferences

**Edit > Preferences...** holds what belongs to the PC and not to the work:

| Setting | Meaning |
|---|---|
| **Unknown register folder** | Where the register of unknowns is kept. **Share the NIAS register** uses the register of a NIAS installation, **Own register** a separate one. |
| **CAS reference (CASINFO.xlsx)** | The table of CAS numbers, limits and references used by the reports. |
| **SpectrAtlas folder** | For **Investigate**; found automatically if empty. |
| **NIST MSSEARCH folder** | For the NIST search; found automatically if empty. |
| **Lib2NIST (Edit library)** | Needed to change NIST user libraries; found automatically if empty. |
| **Ask for a reason for every manual integration (GLP)** | Off unless ticked. The reason goes into the audit trail. |
| **Keep an integration tool active after use** | On: a tool stays chosen until you choose another. |

The window also shows the **data folder**, where methods, layouts, logs and the library index are kept.
