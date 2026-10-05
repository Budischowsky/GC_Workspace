# Identify the peaks

## Tell the program where your libraries are

Once per PC: **Identify > Libraries...**

1. **Add library file...** for an `.msp` file or a Wiley / Shimadzu `.lib` file; **Add folder...** for an
   Agilent `.L` folder, a NIST library folder (mainlib, replib, a user library) or a folder that holds several
   libraries.
2. **Load / check** reads them. The first time a search index is built, which takes a while for large
   libraries (about half a minute for NIST17). Later starts read the index at once.
3. The **Use** column switches a library off without removing it. **Remove** takes it off the list; the files
   stay where they are.

**Take over from SpectrAtlas** adds the libraries of a SpectrAtlas installation on this PC in one go.

## Set up a search method

A **search method** says which libraries are searched, in which order and how. **Identify > Search methods...**

1. Choose a method at the top, or **New...**. **Set as default** makes it the one used when nothing else is
   chosen.
2. Tick the libraries and put them in order with **▲ Up**, **▼ Down**, **To top**, **To bottom** or by dragging.
3. Choose **Library order**: **Combined** searches all ticked libraries together; **Sequential** searches them
   from the top and stops at the first library with a hit at or above the **Stop score**. Sequential is the
   usual choice when your own library of confirmed substances should win over the large commercial ones.
4. Tick **Speed: Fast search** to search many peaks several times faster with the same results.

All fields are explained in [How the library search works](library-search.md#the-settings-that-matter).

## Search all peaks

**Library search...** (`Ctrl+F`) in the toolbar.

| Choice | Meaning |
|---|---|
| **Search method** | The method to use. **Edit...** opens it. The line below shows the library order. |
| **Peaks: TIC peaks** | Searches the peaks of the MS chromatogram. With **Give the names also to the FID peaks at the same time**, each name is copied to the FID peak at the same (delay-corrected) time, so the quantification and the report have it. |
| **Peaks: FID peaks** | Searches the FID peaks; each spectrum is taken from the MS at the FID peak's time. |
| **Scope** | **Active chromatogram** or **All loaded chromatograms**. |
| **Only peaks shown in the peak table** | Searches only the peaks that pass the table's filters - fewer peaks, a faster search. |
| **Spectrum** | How the spectrum of each peak is formed (see below). |
| **Skip peaks that already have a name (unknowns are still searched)** | Leaves named peaks alone; peaks named "unknown ..." are searched again. |
| **Only peaks with a score below** | Searches again only the peaks whose present score is below the number. |
| **Review hits before applying** | Shows all first hits in a table before anything is changed. |

Peaks bound as internal standard and names you typed yourself are never overwritten.

In the **review** table, untick **Apply** for a peak to leave it as it is. Select a peak to see its hit list
on the right, and **Use selected hit for this peak** to take another hit than the first. A first hit below the
quality limit of the search method does not name the peak: it becomes *unknown* or *possible derivative of ...*,
unless you pick a hit yourself.

## How the spectrum of a peak is formed

The box at the top of the **Mass spectrum** panel, and **Spectrum** in the search dialog:

| Mode | The spectrum is |
|---|---|
| **Assigned component, otherwise average minus background** | the component you assigned by deconvolution if there is one; otherwise the average of the scans over the peak minus the scans at its edges. The default. |
| **Apex scan** | the single scan at the top of the peak. |
| **Apex minus start scan (PBM)** | the apex scan minus the scan at the start of the peak, as ChemStation does it. |
| **Deconvoluted component** | the spectrum of the deconvoluted component at the peak - only the ions that belong to it. |
| **Raw scans: average minus background (ignore assignment)** | as the first mode, but never the assigned component. |
| **Raw scans: apex (ignore assignment)** | the apex scan, ignoring any assignment. |

The **Scans** tab of the panel shows which scans were averaged (blue) and which were subtracted as background
(red). Drag the regions and press **Use these scans** to set them yourself for the selected peak;
**Automatic** returns to the automatic choice.

## Work on one peak

Select the peak, then:

| Function | Where | What it does |
|---|---|---|
| **Library hit list (selected peak)** | **Identify** menu, `Ctrl+E` | Searches this peak and shows all hits with forward and reverse score. **Assign hit to peak** takes one. |
| **Library hits** | **Mass Spectrum** menu | The same for the spectrum shown in the **Mass spectrum** panel, with the default search method. |
| **Own library** | **Mass Spectrum** menu | Searches one chosen library only. Choose it under **Own library selection and options**. |
| **Search selected peak in NIST** | **Identify** menu, `Ctrl+N` | Sends the spectrum to NIST MS Search, if installed. |
| **Investigate selected peak in SpectrAtlas...** | **Identify** menu, `Ctrl+Shift+E` | Opens the spectrum in SpectrAtlas for a deeper look, if installed. |
| **Use library hit** | right-click in the peak table | Chooses another hit of the stored hit list as the name. |
| **Clear identification** | right-click in the peak table | Removes the name. |
| Type into **Name** or **CAS** | peak table | Your own identification. It counts as manual and is never overwritten by a search. |

## Read the interpretation

The **Interpretation** tab of the **Mass spectrum** panel gives clues about an unknown - not an
identification: the likely molecular ion, chlorine / bromine / sulfur / silicon from the isotope pattern, ion
series and neutral losses, the most likely substance class (plasticizer, antioxidant, siloxane ...), known
NIAS substances with a matching pattern, and formula suggestions. The optional peak table column
**Class hint** shows the class for every peak.

## Keep an unknown

**Identify > Register selected peak as unknown...** stores the spectrum, retention time and sample of a peak in
the **unknown register**, so that the same unknown is recognised in later samples.

**Identify > Unknown register...** opens the register: find unknowns by text, by sample name or by m/z values
(`149 167 279`), look at the spectrum and where it was seen, search it again, give it a name, **Mark**
several and **Export to MSP...** to share them.

## Add a spectrum to a library

**Identify > Add current spectrum to library...** stores the spectrum now shown in the **Mass spectrum** panel
in one of your own libraries; **Identify > Edit library...** opens the same window to edit or delete entries.
Name, CAS, formula, retention time and retention index are filled in from what is known. NIST user libraries
and MSP libraries can be changed; Agilent `.L`, NIST main and Wiley libraries are read-only. Before every
change the library is copied to a backup folder.

## Retention index

**Identify > Retention index (alkane ladder)...**: choose the run of the n-alkane ladder,
**Read n-alkanes from identifications** fills the table of alkanes and their retention times, and the peak
table can then show an **RI** column. **Report RI column** and **Replace RT by RI in the reports** decide
what the reports show.
