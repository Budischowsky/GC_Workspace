"""Knowledge base of the MS interpreter, kept as plain data.

Everything here uses the same dict schema as the optional user file
``DATA/interpret_rules.json`` (``{"rules": [...], "fingerprints": [...]}``), so
a laboratory can add its own classes and substances without code changes.

Class rule::

    {"id": "phthalate", "label": "Phthalate ester", "group": "Plasticizer",
     "hints": {"O": [4, 4], "rdbe": [6, 10]},    # expected formula ranges (optional)
     "require": [cond, ...],    # all must hold (conditions on unobservable ions are skipped)
     "support": [cond, ...],    # each one that holds raises the score
     "contra":  [cond, ...],    # each one that holds lowers the score
     "examples": "...", "note": "...", "m_weak": true}

Conditions (relative intensities in % of the base peak)::

    {"ion": 149, "min": 30, "max": 100, "w": 1}   ion present in that range
    {"any": [59, 72], "min": 20}                  at least one of the ions
    {"base": [149, 167]}                          base peak is one of them
    {"top": [91, 105], "k": 3}                    one of them among the k largest ions
    {"ratio": [150, 149], "lo": 0.05, "hi": 0.13} intensity ratio I(a)/I(b)
    {"series": "alkyl", "members": 5, "frac": 0.3} ion series (see SERIES)
    {"m": [200, 500]}                             molecular-ion candidate range
    {"parity": "odd"}                             nominal M odd/even (nitrogen rule)
    {"mrel": 40}                                  M candidate at least 40 % of base
    {"loss": 18, "min": 3}                        fragment at M - 18
    {"iso": "Cl", "n": 1}                         isotope pattern evidence for >= n atoms

Fingerprint (substance-level clue)::

    {"name": "DEHP", "cas": "117-81-7", "mw": 390, "cls": "phthalate",
     "ions": [[149, 100], [167, 30], [279, 10], [57, 20]]}

Ion lists follow typical 70 eV EI spectra; relative intensities vary with the
instrument and are only used as a guide (the ranking weighs presence and rank
order more than exact ratios). Every clue must be confirmed by a library
search or a reference standard.
"""
from __future__ import annotations

import json
import logging
from pathlib import Path

log = logging.getLogger(__name__)

# -- ion series ------------------------------------------------------------------

SERIES = {
    "alkyl": {"label": "alkyl ions CnH2n+1⁺ (or acylium CnH2n-1O⁺)",
              "ions": [29, 43, 57, 71, 85, 99, 113, 127, 141, 155, 169, 183, 197, 211, 225]},
    "alkenyl": {"label": "alkenyl / cycloalkyl ions CnH2n-1⁺",
                "ions": [27, 41, 55, 69, 83, 97, 111, 125, 139, 153]},
    "alkene": {"label": "CnH2n⁺· rearrangement ions", "ions": [28, 42, 56, 70, 84, 98, 112, 126, 140]},
    "oxy": {"label": "oxygenated CnH2n+1O⁺ (alcohols, ethers)", "ions": [31, 45, 59, 73, 87, 101, 115]},
    "amine": {"label": "iminium CnH2n+2N⁺ (amines, amides)", "ions": [30, 44, 58, 72, 86, 100, 114]},
    "aromatic": {"label": "aromatic low-mass ions", "ions": [39, 50, 51, 52, 63, 65, 77, 78]},
    "tropylium": {"label": "benzyl / tropylium series", "ions": [91, 105, 119, 133, 147]},
    "siloxane": {"label": "siloxane ions (Si-O)", "ions": [73, 147, 207, 221, 281, 295, 355, 369, 429]},
    "perfluoro": {"label": "perfluoroalkyl ions", "ions": [69, 119, 131, 169, 181, 219, 231]},
    "peg": {"label": "ethoxylate ions (C2H4O)n, Δ44", "ions": [45, 89, 133, 177, 221, 265]},
}

# -- neutral losses from the molecular ion ------------------------------------------

LOSSES = {
    1: "H· (aldehydes, amines, aromatics)",
    2: "H₂",
    15: "CH₃· (methyl, tert-butyl, TMS)",
    16: "O / NH₂· / CH₄",
    17: "OH· (acids, ortho effect) / NH₃",
    18: "H₂O (alcohols, acids)",
    19: "F·",
    20: "HF",
    26: "C₂H₂ (aromatics)",
    27: "HCN (N-aromatics, nitriles)",
    28: "CO (phenols, quinones) / C₂H₄ (McLafferty)",
    29: "CHO· (phenols, aldehydes) / C₂H₅·",
    30: "CH₂O (methoxy aromatics) / NO",
    31: "OCH₃· (methyl esters, methyl ethers)",
    32: "CH₃OH (methyl esters)",
    33: "SH· / H₂O + CH₃",
    34: "H₂S",
    35: "Cl·",
    36: "HCl",
    41: "C₃H₅·",
    42: "CH₂=C=O (acetates, acetamides) / C₃H₆",
    43: "C₃H₇· / CH₃CO· (acetyl)",
    44: "CO₂ (acids, anhydrides) / C₃H₈",
    45: "OC₂H₅· (ethyl esters) / COOH·",
    46: "NO₂ / C₂H₅OH",
    55: "C₄H₇·",
    56: "C₄H₈ (butyl esters)",
    57: "C₄H₉· (butyl, tert-butyl)",
    59: "COOCH₃· (methyl esters) / OC₃H₇·",
    60: "CH₃COOH (acetates)",
    64: "SO₂",
    69: "CF₃·",
    71: "C₅H₁₁·",
    73: "OSi(CH₃)₃· / C₄H₉O·",
    79: "Br·",
    80: "HBr",
    81: "Br·",
    82: "HBr",
    90: "TMS-OH",
    91: "C₇H₇· (benzyl)",
    105: "C₆H₅CO· (benzoyl)",
    127: "I·",
}

#: neutral losses that are chemically implausible from a molecular ion
ILLOGICAL_LOSSES = set(range(3, 15)) | set(range(21, 26))

#: typical background ions: column bleed and air / water
BLEED_IONS = {207, 281, 355, 429, 73, 147, 221, 295}
AIR_IONS = {18, 28, 32, 40, 44}


# -- class rules ----------------------------------------------------------------------

RULES: list[dict] = [
    # -- plasticizers -------------------------------------------------------------------
    {"id": "phthalate", "hints": {"O": [4, 4], "N": [0, 0], "rdbe": [6, 10]}, "label": "Phthalate ester", "group": "Plasticizer",
     "require": [{"ion": 149, "min": 30}],
     "support": [{"base": [149], "w": 2}, {"ion": 150, "min": 4}, {"ratio": [150, 149], "lo": 0.05, "hi": 0.14},
                 {"any": [104, 76, 65], "min": 3}, {"any": [167, 177, 205, 223, 279, 293, 307], "min": 2, "w": 2}],
     "contra": [{"ion": 153, "min": 60}, {"ion": 163, "min": 90}, {"ion": 185, "min": 60}, {"ion": 129, "min": 80}],
     "examples": "DEP (177), DiBP/DBP (223, 205), BBP (91, 206), DEHP (167, 279), DINP (293), DIDP (307)",
     "note": "m/z 149 = protonated phthalic anhydride; the M⁺· of higher phthalates is weak",
     "m_weak": True},
    {"id": "dmp", "hints": {"O": [4, 4], "N": [0, 0], "rdbe": [6, 6]}, "label": "Dimethyl phthalate / methyl benzoate ester", "group": "Plasticizer",
     "require": [{"ion": 163, "min": 60}],
     "support": [{"base": [163], "w": 2}, {"ion": 194, "min": 2}, {"ion": 77, "min": 10}, {"ion": 133, "min": 3}],
     "contra": [{"ion": 149, "min": 60}],
     "examples": "dimethyl phthalate, dimethyl terephthalate (163, 194)"},
    {"id": "phthalate_d4", "hints": {"O": [4, 4]}, "label": "Ring-d4 phthalate (deuterated internal standard)", "group": "Internal standard",
     "require": [{"ion": 153, "min": 30}],
     "support": [{"base": [153], "w": 2}, {"any": [171, 108], "min": 2, "w": 2}, {"ion": 149, "max": 20}],
     "contra": [{"ion": 149, "min": 60}],
     "examples": "BBP-d4, DnNP-d4 (m/z 153 = 149 + 4)", "m_weak": True},
    {"id": "terephthalate", "hints": {"O": [4, 4], "N": [0, 0], "rdbe": [6, 6]}, "label": "Terephthalate ester (DEHT type)", "group": "Plasticizer",
     "require": [{"ion": 261, "min": 5}, {"ion": 112, "min": 15}],
     "support": [{"base": [70, 112, 57], "w": 2}, {"ion": 149, "min": 5}, {"ion": 167, "min": 3}, {"ion": 279, "max": 5}],
     "contra": [{"ion": 149, "min": 80}],
     "examples": "bis(2-ethylhexyl) terephthalate (DEHT/DOTP)"},
    {"id": "adipate", "hints": {"O": [4, 4], "N": [0, 0], "rdbe": [2, 2]}, "label": "Adipate ester", "group": "Plasticizer",
     "require": [{"ion": 129, "min": 35}],
     "support": [{"base": [129], "w": 2}, {"ion": 147, "min": 5}, {"ion": 111, "min": 5}, {"ion": 112, "min": 10},
                 {"series": "alkyl", "members": 3, "frac": 0.1}],
     "contra": [{"ion": 149, "min": 40}, {"ion": 185, "min": 40}, {"ion": 60, "min": 30}],
     "examples": "DEHA (129, 112, 147), diisobutyl/dibutyl adipate (129, 185)", "m_weak": True},
    {"id": "citrate", "hints": {"O": [7, 8], "N": [0, 0]}, "label": "Citrate ester", "group": "Plasticizer",
     "require": [{"ion": 185, "min": 40}, {"ion": 129, "min": 20}],
     "support": [{"base": [185], "w": 2}, {"ion": 259, "min": 5, "w": 2}, {"ion": 157, "min": 5}, {"ion": 43, "min": 20}],
     "examples": "acetyl tributyl citrate ATBC (185, 129, 259), tributyl citrate", "m_weak": True},
    {"id": "cyclohexanedicarboxylate", "label": "Cyclohexane-1,2-dicarboxylate (DINCH type)", "group": "Plasticizer",
     "require": [{"ion": 155, "min": 30}, {"ion": 127, "min": 10}],
     "support": [{"base": [155], "w": 2}, {"ion": 173, "min": 5}, {"ion": 281, "min": 1}],
     "examples": "DINCH (155, 127, 173, 281)", "m_weak": True},
    {"id": "benzoate", "hints": {"O": [2, 4], "rdbe": [5, 12]}, "label": "Benzoate ester / benzoic acid derivative", "group": "Additive",
     "require": [{"ion": 105, "min": 50}, {"ion": 77, "min": 15}],
     "support": [{"ion": 51, "min": 5}, {"any": [122, 123], "min": 3, "w": 2}, {"base": [105]}],
     "contra": [{"ion": 182, "min": 20}, {"ion": 91, "min": 40}, {"ion": 99, "min": 60}],
     "examples": "methyl/2-ethylhexyl benzoate, dibenzoates (105, 77, 123)"},
    # -- fatty compounds ------------------------------------------------------------------
    {"id": "fame", "hints": {"O": [2, 2], "N": [0, 0], "rdbe": [1, 1]}, "label": "Fatty acid methyl ester", "group": "Fatty compound",
     "require": [{"ion": 74, "min": 40}, {"ion": 87, "min": 15}],
     "support": [{"base": [74], "w": 2}, {"ion": 143, "min": 3}, {"loss": 31, "min": 1}, {"loss": 43, "min": 1},
                 {"series": "alkyl", "members": 3, "frac": 0.1}],
     "examples": "methyl palmitate (74, 87, 143, M 270), methyl stearate (M 298)"},
    {"id": "fame_unsat", "hints": {"O": [2, 2], "N": [0, 0], "rdbe": [2, 4]}, "label": "Unsaturated fatty acid methyl ester", "group": "Fatty compound",
     "require": [{"ion": 74, "min": 15}, {"series": "alkenyl", "members": 4, "frac": 0.2}],
     "support": [{"ion": 87, "min": 10}, {"loss": 32, "min": 2}, {"base": [55, 67, 79]}],
     "examples": "methyl oleate (55, 74, 264, M 296), methyl linoleate (67, 81, 294)"},
    {"id": "ethyl_ester", "hints": {"O": [2, 2], "N": [0, 0], "rdbe": [1, 2]}, "label": "Fatty acid ethyl ester", "group": "Fatty compound",
     "require": [{"ion": 88, "min": 40}, {"ion": 101, "min": 15}],
     "support": [{"base": [88], "w": 2}, {"loss": 45, "min": 1}, {"ion": 157, "min": 3}],
     "examples": "ethyl palmitate (88, 101, M 284)"},
    {"id": "fatty_acid", "hints": {"O": [2, 2], "N": [0, 0], "rdbe": [1, 3]}, "label": "Carboxylic acid (fatty acid)", "group": "Fatty compound",
     "require": [{"ion": 60, "min": 25}, {"ion": 73, "min": 25}],
     "support": [{"ion": 129, "min": 5}, {"parity": "even"}, {"series": "alkyl", "members": 3, "frac": 0.1},
                 {"loss": 17, "min": 1}, {"loss": 45, "min": 1}],
     "contra": [{"ion": 147, "min": 10}],
     "examples": "palmitic acid (60, 73, 129, 256), stearic acid (284)"},
    {"id": "fatty_amide", "hints": {"O": [1, 1], "N": [1, 1], "rdbe": [1, 3]}, "label": "Primary fatty acid amide (slip agent)", "group": "Slip agent",
     "require": [{"ion": 59, "min": 35}, {"ion": 72, "min": 20}],
     "support": [{"base": [59], "w": 2}, {"parity": "odd", "w": 2}, {"ion": 86, "min": 3}, {"ion": 114, "min": 2},
                 {"ion": 128, "min": 1}],
     "contra": [{"ion": 105, "min": 30}, {"ion": 73, "min": 50}],
     "examples": "oleamide (M 281), erucamide (M 337), stearamide (M 283)", "m_weak": True},
    {"id": "alcohol", "label": "Aliphatic alcohol", "group": "Oxygenated",
     "require": [{"ion": 31, "min": 10}],
     "support": [{"series": "oxy", "members": 2, "frac": 0.15}, {"loss": 18, "min": 2},
                 {"series": "alkenyl", "members": 3, "frac": 0.15}],
     "contra": [{"ion": 74, "min": 30}, {"ion": 149, "min": 30}],
     "examples": "1-alkanols, 2-ethylhexanol", "m_weak": True},
    # -- hydrocarbons -----------------------------------------------------------------------
    {"id": "alkane", "hints": {"O": [0, 0], "N": [0, 0], "rdbe": [0, 0]}, "label": "Alkane (n- or branched; POSH/MOSH)", "group": "Hydrocarbon",
     "require": [{"series": "alkyl", "members": 5, "frac": 0.35}, {"base": [43, 57, 71]}],
     "support": [{"ion": 85, "min": 10}, {"ion": 99, "min": 4}, {"ion": 113, "min": 2}, {"ratio": [71, 57], "lo": 0.3, "hi": 0.9}],
     "contra": [{"ion": 91, "min": 20}, {"ion": 149, "min": 20}, {"ion": 74, "min": 30}, {"ion": 60, "min": 30},
                {"iso": "Cl", "n": 1}],
     "examples": "n-alkanes, isoalkanes, polyolefin oligomers (POSH), mineral oil (MOSH)",
     "note": "n-alkanes: smooth CnH2n+1 series and a weak M⁺·; branching raises the ions at the branch "
             "and removes M⁺·; the retention index gives the chain length", "m_weak": True},
    {"id": "alkene", "hints": {"N": [0, 0], "rdbe": [1, 2]}, "label": "Alkene / cycloalkane / long-chain alcohol", "group": "Hydrocarbon",
     "require": [{"series": "alkenyl", "members": 4, "frac": 0.25}, {"base": [41, 55, 69, 83, 97]}],
     "support": [{"series": "alkene", "members": 3, "frac": 0.08}, {"ion": 83, "min": 20}, {"ion": 97, "min": 10}],
     "contra": [{"ion": 91, "min": 30}, {"ion": 74, "min": 30}, {"ion": 59, "min": 40}, {"ion": 81, "min": 50}],
     "examples": "1-alkenes, alkylcyclohexanes, polyolefin oligomers, fatty alcohols (M−18)", "m_weak": True},
    {"id": "alkylbenzene", "hints": {"O": [0, 0], "N": [0, 0], "rdbe": [4, 5]}, "label": "Alkylbenzene (MOAH type)", "group": "Hydrocarbon",
     "require": [{"ion": 91, "min": 40}],
     "support": [{"base": [91, 105, 119], "w": 2}, {"ion": 92, "min": 8}, {"ion": 65, "min": 5}, {"ion": 77, "min": 3},
                 {"series": "tropylium", "members": 2, "frac": 0.2}, {"parity": "even"}],
     "contra": [{"ion": 149, "min": 30}, {"ion": 104, "min": 50}, {"ion": 206, "min": 15}],
     "examples": "toluene, xylenes, alkylbenzenes (MOAH)"},
    {"id": "styrene", "hints": {"O": [0, 0], "N": [0, 0], "rdbe": [5, 16]}, "label": "Styrene / styrene oligomer", "group": "Monomer / oligomer",
     "require": [{"ion": 104, "min": 25}],
     "support": [{"ion": 91, "min": 30}, {"ion": 78, "min": 10}, {"ion": 103, "min": 15}, {"ion": 117, "min": 10},
                 {"any": [208, 312, 196], "min": 2, "w": 2}],
     "examples": "styrene (104), 2,4-diphenyl-1-butene (208), 2,4,6-triphenyl-1-hexene (312)"},
    {"id": "pah", "hints": {"O": [0, 0], "N": [0, 0], "rdbe": [7, 20]}, "label": "Polycyclic aromatic hydrocarbon", "group": "Hydrocarbon",
     "require": [{"mrel": 80}, {"m": [128, 302]}, {"parity": "even"}],
     "support": [{"loss": 1, "min": 10}, {"loss": 26, "max": 15}],
     "contra": [{"iso": "Cl", "n": 1}, {"iso": "S", "n": 1}, {"ion": 57, "min": 20}],
     "examples": "naphthalene (128), phenanthrene (178), pyrene (202)"},
    {"id": "monoterpene", "hints": {"O": [0, 0], "N": [0, 0], "rdbe": [2, 3]}, "label": "Monoterpene (C10H16)", "group": "Natural product",
     "require": [{"ion": 93, "min": 35}, {"ion": 136, "min": 3}],
     "support": [{"ion": 121, "min": 8}, {"ion": 68, "min": 15}, {"ion": 79, "min": 15}, {"m": [136, 136]}],
     "examples": "limonene (68, 93, 136), α-pinene (93, 136)"},
    {"id": "sesquiterpene", "hints": {"O": [0, 0], "N": [0, 0], "rdbe": [3, 5]}, "label": "Sesquiterpene (C15H24)", "group": "Natural product",
     "require": [{"ion": 204, "min": 3}, {"any": [161, 189, 119, 105, 93], "min": 30}],
     "support": [{"m": [204, 204]}, {"ion": 133, "min": 10}],
     "examples": "caryophyllene, cadinene"},
    {"id": "squalene", "label": "Squalene / polyisoprenoid", "group": "Natural product",
     "require": [{"ion": 69, "min": 60}, {"ion": 81, "min": 25}],
     "support": [{"base": [69], "w": 2}, {"ion": 137, "min": 3}, {"ion": 95, "min": 10}, {"ion": 121, "min": 3}],
     "contra": [{"any": [119, 131, 169], "min": 10}],
     "examples": "squalene (M 410)", "m_weak": True},
    # -- antioxidants and degradation products ----------------------------------------------
    {"id": "hindered_phenol", "hints": {"O": [1, 3], "N": [0, 0], "rdbe": [3, 6]}, "label": "tert-Butylphenol antioxidant or degradation product", "group": "Antioxidant",
     "require": [{"ion": 57, "min": 8}, {"any": [191, 205, 219, 277, 203, 177], "min": 50}],
     "support": [{"loss": 15, "min": 30, "w": 2}, {"base": [191, 205, 219, 277, 177], "w": 2},
                 {"any": [206, 220, 234, 292], "min": 5}],
     "contra": [{"ion": 149, "min": 60}, {"ion": 441, "min": 40}],
     "examples": "BHT (205, 220), 2,4-di-tert-butylphenol (191, 206), 3,5-di-tBu-4-hydroxybenzaldehyde (219, 234), "
                 "Irganox 1010/1076 degradation (277, 292)"},
    {"id": "tert_butyl_aromatic", "label": "tert-Butylated phenol / aromatic (antioxidant family)",
     "group": "Antioxidant", "hints": {"O": [1, 4]},
     "require": [{"loss": 15, "min": 40}, {"ion": 57, "min": 5}],
     "support": [{"parity": "even"}, {"any": [147, 219, 203, 191, 205, 177, 161], "min": 10, "w": 2}, {"mrel": 10}],
     "contra": [{"ion": 73, "min": 40}, {"iso": "Si", "n": 1}, {"ion": 149, "min": 60}],
     "examples": "esters of 3-(3,5-di-tert-butyl-4-hydroxyphenyl)propionic acid, "
                 "3,5-di-tert-butyl-4-hydroxybenzoates (M−15 = loss of CH₃ from tert-butyl)"},
    {"id": "oxaspiro", "label": "7,9-Di-tert-butyl-1-oxaspiro[4.5]deca-6,9-diene-2,8-dione (antioxidant degradation)",
     "group": "Antioxidant",
     "require": [{"ion": 205, "min": 40}, {"ion": 217, "min": 15}],
     "support": [{"ion": 232, "min": 8, "w": 2}, {"ion": 276, "min": 3, "w": 2}, {"ion": 175, "min": 5}, {"ion": 57, "min": 20}],
     "examples": "CAS 82304-66-3 (M 276)"},
    {"id": "phosphite", "label": "Aryl phosphite antioxidant (Irgafos 168 type)", "group": "Antioxidant",
     "require": [{"ion": 441, "min": 40}],
     "support": [{"ion": 57, "min": 10}, {"ion": 646, "min": 1, "w": 2}, {"ion": 147, "min": 3}],
     "examples": "tris(2,4-di-tert-butylphenyl) phosphite (441, M 646)"},
    {"id": "phosphate_ao", "label": "Oxidised Irgafos 168 (aryl phosphate)", "group": "Antioxidant",
     "require": [{"ion": 647, "min": 40}],
     "support": [{"ion": 662, "min": 5, "w": 2}, {"ion": 57, "min": 10}],
     "examples": "tris(2,4-di-tert-butylphenyl) phosphate (647, M 662)"},
    {"id": "benzotriazole", "label": "Phenolic benzotriazole UV absorber", "group": "UV absorber",
     "require": [{"loss": 15, "min": 60}, {"parity": "odd"}, {"m": [220, 460]}],
     "support": [{"ion": 57, "min": 5}, {"mrel": 15}],
     "examples": "UV-326 (300, M 315), UV-328 (322, M 351), UV-327 (342, M 357)"},
    # -- photoinitiators / ketones ------------------------------------------------------------
    {"id": "aryl_ketone", "hints": {"O": [1, 2], "N": [0, 0], "rdbe": [5, 10]}, "label": "Aryl ketone / benzophenone-type photoinitiator", "group": "Photoinitiator",
     "require": [{"ion": 105, "min": 40}, {"ion": 77, "min": 15}],
     "support": [{"any": [182, 196, 258, 120], "min": 10, "w": 2}, {"ion": 51, "min": 5}, {"parity": "even"},
                 {"any": [99, 59], "min": 50}],
     "contra": [{"any": [122, 123], "min": 20}, {"ion": 91, "min": 40}],
     "examples": "benzophenone (105, 182), 4-methylbenzophenone (119, 196), Irgacure 184 (99, 105), "
                 "Darocur 1173 (59, 105), acetophenone (105, 120)"},
    {"id": "thioxanthone", "label": "Thioxanthone photoinitiator", "group": "Photoinitiator",
     "require": [{"any": [239, 254, 212], "min": 50}],
     "support": [{"iso": "S", "n": 1, "w": 2}, {"ion": 197, "min": 5}, {"ion": 184, "min": 3}],
     "examples": "2-/4-isopropylthioxanthone ITX (239, M 254), thioxanthone (212)"},
    {"id": "aminobenzoate", "label": "Dialkylaminobenzoate amine synergist", "group": "Photoinitiator",
     "require": [{"ion": 148, "min": 40}],
     "support": [{"any": [164, 165], "min": 15, "w": 2}, {"any": [193, 277], "min": 5, "w": 2}, {"parity": "odd"}],
     "examples": "EDB ethyl 4-(dimethylamino)benzoate (148, 193), EHDAB (165, 277)"},
    {"id": "methyl_ketone", "label": "Methyl ketone", "group": "Oxygenated",
     "require": [{"ion": 43, "min": 50}, {"ion": 58, "min": 15}],
     "support": [{"base": [43, 58], "w": 2}, {"ion": 71, "min": 5}, {"loss": 15, "min": 1}],
     "contra": [{"ion": 74, "min": 30}, {"ion": 60, "min": 30}],
     "examples": "2-alkanones (43, McLafferty 58)"},
    {"id": "aldehyde", "label": "Aliphatic aldehyde", "group": "Oxygenated",
     "require": [{"ion": 44, "min": 15}, {"any": [41, 43, 57], "min": 40}],
     "support": [{"ion": 82, "min": 5}, {"loss": 18, "min": 1}, {"loss": 44, "min": 1}, {"loss": 28, "min": 1}],
     "contra": [{"ion": 74, "min": 30}, {"ion": 58, "min": 40}, {"ion": 60, "min": 30}],
     "examples": "hexanal … decanal (44, 41, 57, 82)", "m_weak": True},
    {"id": "acetate", "label": "Acetate ester", "group": "Oxygenated",
     "require": [{"ion": 43, "min": 70}, {"ion": 61, "min": 2}],
     "support": [{"base": [43], "w": 2}, {"loss": 60, "min": 3, "w": 2}, {"loss": 42, "min": 1}],
     "contra": [{"ion": 58, "min": 40}],
     "examples": "butyl acetate (43, 56, 61, 73), triacetin (43, 103, 145)", "m_weak": True},
    {"id": "lactone", "label": "γ-Lactone", "group": "Oxygenated",
     "require": [{"ion": 85, "min": 60}], "support": [{"base": [85], "w": 2}, {"ion": 29, "min": 5}, {"ion": 56, "min": 5}],
     "contra": [{"ion": 57, "min": 80}],
     "examples": "γ-butyrolactone, γ-alkyl lactones (85)"},
    {"id": "phenol", "label": "Phenol / hydroxyaromatic", "group": "Oxygenated",
     "require": [{"mrel": 40}, {"loss": 28, "min": 5}],
     "support": [{"loss": 29, "min": 5}, {"parity": "even"}, {"ion": 65, "min": 5}, {"ion": 39, "min": 5}],
     "contra": [{"ion": 57, "min": 40}],
     "examples": "phenol (94), cresols (107, 108)"},
    {"id": "alkylphenol", "label": "Alkylphenol (nonyl-/octylphenol type)", "group": "Additive",
     "require": [{"ion": 135, "min": 40}, {"ion": 107, "min": 10}],
     "support": [{"any": [121, 149], "min": 10}, {"any": [206, 220], "min": 1, "w": 2}],
     "contra": [{"ion": 91, "min": 50}],
     "examples": "4-tert-octylphenol (135, 206), 4-nonylphenol (135, 149, 220)"},
    {"id": "bisphenol", "label": "Bisphenol A / BPA derivative", "group": "Monomer / oligomer",
     "require": [{"ion": 213, "min": 50}],
     "support": [{"ion": 228, "min": 10, "w": 2}, {"ion": 119, "min": 10}, {"base": [213]}],
     "examples": "bisphenol A (213, 228), BADGE (325, 340)"},
    {"id": "glycol_ether", "label": "Glycol ether / ethoxylate", "group": "Oxygenated",
     "require": [{"ion": 45, "min": 30}, {"any": [57, 59, 89, 75], "min": 10}],
     "support": [{"series": "peg", "members": 2, "frac": 0.2, "w": 2}, {"ion": 89, "min": 5}, {"ion": 133, "min": 3}],
     "contra": [{"ion": 60, "min": 30}],
     "examples": "2-butoxyethanol (57, 45, 87), butyl diglycol (45, 57, 75), ethoxylates", "m_weak": True},
    # -- nitrogen ------------------------------------------------------------------------------
    {"id": "caprolactam", "hints": {"O": [1, 2], "N": [1, 2]}, "label": "Caprolactam / polyamide 6 oligomer", "group": "Monomer / oligomer",
     "require": [{"ion": 113, "min": 40}, {"ion": 55, "min": 25}],
     "support": [{"ion": 85, "min": 20}, {"ion": 56, "min": 20}, {"ion": 84, "min": 15}, {"parity": "odd"},
                 {"ion": 226, "min": 5, "w": 2}],
     "examples": "ε-caprolactam (M 113), cyclic PA6 dimer (M 226)"},
    {"id": "aromatic_amine", "label": "Aromatic amine (primary aromatic amine?)", "group": "Nitrogen compound",
     "require": [{"parity": "odd"}, {"mrel": 50}, {"m": [93, 260]}],
     "support": [{"loss": 1, "min": 20}, {"loss": 27, "min": 5, "w": 2}, {"ion": 65, "min": 5}],
     "contra": [{"ion": 57, "min": 40}, {"ion": 43, "min": 50}],
     "examples": "aniline (93), toluidines (107), 2,4-TDA (122), 4,4'-MDA (198)"},
    {"id": "aliphatic_amine", "label": "Aliphatic amine (α-cleavage iminium ion)", "group": "Nitrogen compound",
     "require": [{"base": [30, 44, 58, 72, 86, 100]}, {"parity": "odd"}],
     "support": [{"series": "amine", "members": 3, "frac": 0.3, "w": 2}],
     "examples": "alkylamines, dialkylamines"},
    {"id": "benzothiazole", "label": "Benzothiazole (rubber accelerator residue)", "group": "Additive",
     "require": [{"any": [135, 181, 167], "min": 60}, {"any": [108, 148, 136], "min": 15}],
     "support": [{"iso": "S", "n": 1, "w": 2}, {"ion": 69, "min": 5}, {"parity": "odd"}],
     "examples": "benzothiazole (135, 108), 2-(methylthio)benzothiazole (181, 148)"},
    {"id": "isocyanate", "label": "Aromatic isocyanate", "group": "Monomer / oligomer",
     "require": [{"mrel": 80}, {"any": [174, 250], "min": 80}],
     "support": [{"loss": 29, "min": 20}, {"ion": 145, "min": 20}],
     "examples": "2,4-TDI (174, 145), 4,4'-MDI (250)"},
    # -- silicon -------------------------------------------------------------------------------
    {"id": "siloxane_cyclic", "label": "Cyclic siloxane / column bleed", "group": "Siloxane",
     "require": [{"any": [207, 281, 355, 429, 341], "min": 40}],
     "support": [{"ion": 73, "min": 5}, {"iso": "Si", "n": 1, "w": 2}, {"series": "siloxane", "members": 3, "frac": 0.4, "w": 2},
                 {"any": [191, 265, 267, 327], "min": 5}, {"ratio": [283, 281], "lo": 0.08, "hi": 0.3, "w": 2}],
     "contra": [{"ratio": [283, 281], "hi": 0.05, "min_b": 20, "w": 2},
                {"ratio": [209, 207], "hi": 0.04, "min_b": 20, "w": 2}],
     "examples": "D3 (207), D4 (281), D5 (355), D6 (341, 429); column bleed (207, 281)",
     "note": "cyclic siloxanes show M−15 as base peak; the M⁺· itself is weak", "m_weak": True},
    {"id": "siloxane_linear", "label": "Linear siloxane / TMS derivative", "group": "Siloxane",
     "require": [{"ion": 73, "min": 50}, {"any": [147, 75, 221], "min": 5}],
     "support": [{"loss": 15, "min": 5, "w": 2}, {"iso": "Si", "n": 1}, {"series": "siloxane", "members": 3, "frac": 0.3}],
     "contra": [{"ion": 60, "min": 40}],
     "examples": "linear polydimethylsiloxanes (73, 147, 221), TMS ethers/esters (73, 75, M−15)", "m_weak": True},
    # -- heteroatoms -----------------------------------------------------------------------------
    {"id": "chlorinated", "label": "Chlorinated compound", "group": "Halogenated",
     "require": [{"iso": "Cl", "n": 1}], "support": [{"loss": 35, "min": 3}, {"loss": 36, "min": 3}],
     "examples": "chloroalkanes, chloroaromatics, TCPP"},
    {"id": "brominated", "label": "Brominated compound (flame retardant?)", "group": "Halogenated",
     "require": [{"iso": "Br", "n": 1}], "support": [{"loss": 79, "min": 3}, {"loss": 80, "min": 3}],
     "examples": "bromophenols, brominated flame retardants"},
    {"id": "sulfur", "label": "Sulphur compound", "group": "Heteroatom",
     "require": [{"iso": "S", "n": 1}], "support": [{"any": [45, 47, 64], "min": 5}, {"loss": 34, "min": 3}],
     "examples": "sulphides, thiophenes, benzothiazoles, thioxanthones"},
    {"id": "alkyl_phosphate", "label": "Alkyl phosphate (TBP type)", "group": "Flame retardant / plasticizer",
     "require": [{"ion": 99, "min": 60}, {"any": [155, 211, 125], "min": 5}],
     "support": [{"base": [99], "w": 2}, {"ion": 57, "min": 5}],
     "examples": "tributyl phosphate (99, 155, 211), TCPP (99, 125, 277)"},
    {"id": "perfluoro", "label": "Perfluorinated compound", "group": "Halogenated",
     "require": [{"ion": 69, "min": 40}, {"any": [119, 131, 169], "min": 10}],
     "support": [{"series": "perfluoro", "members": 3, "frac": 0.3, "w": 2}],
     "contra": [{"series": "alkenyl", "members": 5, "frac": 0.4}],
     "examples": "fluorotelomers, perfluoroalkanes (69, 119, 131, 169)"},
]

# -- substance fingerprints ------------------------------------------------------------

FINGERPRINTS: list[dict] = [
    # phthalates and other plasticizers
    {"name": "Dimethyl phthalate (DMP)", "cas": "131-11-3", "mw": 194, "cls": "dmp",
     "ions": [[163, 100], [77, 20], [164, 10], [194, 6], [133, 6], [76, 8], [92, 5]]},
    {"name": "Diethyl phthalate (DEP)", "cas": "84-66-2", "mw": 222, "cls": "phthalate",
     "ions": [[149, 100], [177, 25], [150, 10], [176, 7], [105, 6], [65, 6], [121, 5], [222, 3]]},
    {"name": "Diisobutyl phthalate (DiBP)", "cas": "84-69-5", "mw": 278, "cls": "phthalate",
     "ions": [[149, 100], [57, 15], [223, 10], [41, 10], [150, 9], [104, 5], [205, 3], [167, 3]]},
    {"name": "Dibutyl phthalate (DBP)", "cas": "84-74-2", "mw": 278, "cls": "phthalate",
     "ions": [[149, 100], [150, 10], [41, 8], [223, 5], [205, 4], [104, 5], [76, 5], [57, 4]]},
    {"name": "Benzyl butyl phthalate (BBP)", "cas": "85-68-7", "mw": 312, "cls": "phthalate",
     "ions": [[149, 100], [91, 70], [206, 25], [65, 10], [104, 8], [150, 9], [238, 3]]},
    {"name": "Bis(2-ethylhexyl) phthalate (DEHP)", "cas": "117-81-7", "mw": 390, "cls": "phthalate",
     "ions": [[149, 100], [167, 30], [57, 20], [71, 15], [279, 10], [150, 10], [113, 8], [70, 8], [83, 6]]},
    {"name": "Diisononyl phthalate (DINP)", "cas": "28553-12-0", "mw": 418, "cls": "phthalate",
     "ions": [[149, 100], [293, 15], [57, 25], [71, 20], [127, 8], [150, 10], [167, 5]]},
    {"name": "Diisodecyl phthalate (DIDP)", "cas": "26761-40-0", "mw": 446, "cls": "phthalate",
     "ions": [[149, 100], [307, 15], [57, 25], [71, 20], [141, 8], [150, 10], [167, 5]]},
    {"name": "Bis(2-ethylhexyl) terephthalate (DEHT)", "cas": "6422-86-2", "mw": 390, "cls": "terephthalate",
     "ions": [[70, 100], [112, 55], [57, 50], [261, 35], [71, 30], [83, 25], [149, 15], [167, 15]]},
    {"name": "Bis(2-ethylhexyl) adipate (DEHA)", "cas": "103-23-1", "mw": 370, "cls": "adipate",
     "ions": [[129, 100], [57, 50], [112, 40], [71, 35], [70, 30], [147, 25], [55, 20], [83, 15], [241, 5]]},
    {"name": "Acetyl tributyl citrate (ATBC)", "cas": "77-90-7", "mw": 402, "cls": "citrate",
     "ions": [[185, 100], [43, 60], [129, 80], [57, 40], [259, 40], [157, 15], [213, 10]]},
    {"name": "Tributyl citrate", "cas": "77-94-1", "mw": 360, "cls": "citrate",
     "ions": [[185, 100], [129, 80], [57, 40], [259, 30], [41, 20], [157, 15]]},
    {"name": "Diisononyl cyclohexane-1,2-dicarboxylate (DINCH)", "cas": "166412-78-8", "mw": 424,
     "cls": "cyclohexanedicarboxylate", "ions": [[155, 100], [127, 30], [57, 25], [71, 20], [173, 10], [281, 3]]},
    {"name": "Triacetin", "cas": "102-76-1", "mw": 218, "cls": "acetate",
     "ions": [[43, 100], [103, 30], [145, 30], [116, 15], [86, 10]]},
    # fatty compounds and slip agents
    {"name": "Methyl palmitate", "cas": "112-39-0", "mw": 270, "cls": "fame",
     "ions": [[74, 100], [87, 60], [43, 50], [55, 30], [143, 12], [227, 10], [270, 8], [239, 5]]},
    {"name": "Methyl stearate", "cas": "112-61-8", "mw": 298, "cls": "fame",
     "ions": [[74, 100], [87, 70], [43, 50], [143, 15], [255, 8], [298, 10], [267, 5]]},
    {"name": "Methyl oleate", "cas": "112-62-9", "mw": 296, "cls": "fame_unsat",
     "ions": [[55, 100], [69, 60], [74, 50], [83, 50], [97, 40], [264, 10], [296, 3]]},
    {"name": "Ethyl palmitate", "cas": "628-97-7", "mw": 284, "cls": "ethyl_ester",
     "ions": [[88, 100], [101, 60], [43, 40], [55, 30], [157, 10], [284, 6], [239, 5]]},
    {"name": "Palmitic acid", "cas": "57-10-3", "mw": 256, "cls": "fatty_acid",
     "ions": [[73, 100], [60, 85], [43, 90], [129, 30], [256, 35], [213, 15], [185, 10]]},
    {"name": "Stearic acid", "cas": "57-11-4", "mw": 284, "cls": "fatty_acid",
     "ions": [[73, 100], [60, 80], [43, 90], [129, 30], [284, 35], [241, 12], [185, 10]]},
    {"name": "Oleamide", "cas": "301-02-0", "mw": 281, "cls": "fatty_amide",
     "ions": [[59, 100], [72, 70], [55, 50], [41, 45], [43, 40], [69, 20], [83, 15], [98, 10], [114, 6], [281, 5]]},
    {"name": "Erucamide", "cas": "112-84-5", "mw": 337, "cls": "fatty_amide",
     "ions": [[59, 100], [72, 70], [55, 50], [41, 40], [43, 40], [69, 15], [83, 12], [337, 3]]},
    {"name": "Stearamide", "cas": "124-26-5", "mw": 283, "cls": "fatty_amide",
     "ions": [[59, 100], [72, 60], [43, 40], [57, 25], [86, 15], [114, 10], [128, 5], [283, 5]]},
    {"name": "Palmitamide", "cas": "629-54-9", "mw": 255, "cls": "fatty_amide",
     "ions": [[59, 100], [72, 60], [43, 40], [57, 25], [86, 15], [114, 10], [255, 5]]},
    {"name": "2-Ethylhexanol", "cas": "104-76-7", "mw": 130, "cls": "",
     "ions": [[57, 100], [41, 50], [43, 45], [55, 30], [70, 30], [83, 25], [98, 5], [112, 3]]},
    # antioxidants and degradation products
    {"name": "BHT (2,6-di-tert-butyl-4-methylphenol)", "cas": "128-37-0", "mw": 220, "cls": "hindered_phenol",
     "ions": [[205, 100], [220, 25], [57, 25], [206, 15], [145, 10], [177, 5], [41, 10]]},
    {"name": "2,4-Di-tert-butylphenol", "cas": "96-76-4", "mw": 206, "cls": "hindered_phenol",
     "ions": [[191, 100], [206, 25], [57, 30], [192, 15], [41, 10], [163, 5], [74, 5]]},
    {"name": "2,6-Di-tert-butyl-p-benzoquinone", "cas": "719-22-2", "mw": 220, "cls": "hindered_phenol",
     "ions": [[177, 100], [220, 80], [135, 70], [205, 50], [67, 40], [91, 30], [107, 25]]},
    {"name": "3,5-Di-tert-butyl-4-hydroxybenzaldehyde", "cas": "1620-98-0", "mw": 234, "cls": "hindered_phenol",
     "ions": [[219, 100], [234, 25], [57, 30], [191, 10], [220, 15]]},
    {"name": "Methyl 3-(3,5-di-tert-butyl-4-hydroxyphenyl)propionate", "cas": "6386-38-5", "mw": 292,
     "cls": "hindered_phenol", "ions": [[277, 100], [292, 40], [57, 40], [147, 10], [219, 10], [278, 20]]},
    {"name": "7,9-Di-tert-butyl-1-oxaspiro[4.5]deca-6,9-diene-2,8-dione", "cas": "82304-66-3", "mw": 276,
     "cls": "oxaspiro", "ions": [[205, 100], [57, 60], [217, 40], [232, 25], [175, 20], [161, 15], [276, 10]]},
    {"name": "Irgafos 168 (tris(2,4-di-tert-butylphenyl) phosphite)", "cas": "31570-04-4", "mw": 646, "cls": "phosphite",
     "ions": [[441, 100], [57, 40], [147, 15], [191, 8], [646, 5], [308, 5]]},
    {"name": "Tris(2,4-di-tert-butylphenyl) phosphate (oxidised Irgafos 168)", "cas": "95906-11-9", "mw": 662,
     "cls": "phosphate_ao", "ions": [[647, 100], [57, 30], [662, 15], [191, 10]]},
    {"name": "Irganox 1076", "cas": "2082-79-3", "mw": 530, "cls": "hindered_phenol",
     "ions": [[57, 100], [530, 70], [219, 60], [515, 15], [203, 10], [277, 10]]},
    {"name": "UV-326 (bumetrizole)", "cas": "3896-11-5", "mw": 315, "cls": "benzotriazole",
     "ions": [[300, 100], [315, 35], [272, 10], [57, 10], [302, 30]]},
    {"name": "UV-328", "cas": "25973-55-1", "mw": 351, "cls": "benzotriazole",
     "ions": [[322, 100], [351, 20], [336, 10], [57, 15], [323, 25]]},
    {"name": "UV-327", "cas": "3864-99-1", "mw": 357, "cls": "benzotriazole",
     "ions": [[342, 100], [357, 40], [57, 10], [344, 35]]},
    # photoinitiators and aromatic ketones
    {"name": "Benzophenone", "cas": "119-61-9", "mw": 182, "cls": "aryl_ketone",
     "ions": [[105, 100], [77, 80], [182, 60], [51, 25], [181, 25], [152, 5]]},
    {"name": "4-Methylbenzophenone", "cas": "134-84-9", "mw": 196, "cls": "aryl_ketone",
     "ions": [[119, 100], [196, 60], [105, 40], [91, 40], [77, 35], [65, 20], [181, 20]]},
    {"name": "4-Phenylbenzophenone", "cas": "2128-93-0", "mw": 258, "cls": "aryl_ketone",
     "ions": [[181, 100], [258, 65], [105, 40], [152, 40], [77, 30]]},
    {"name": "1-Hydroxycyclohexyl phenyl ketone (Irgacure 184)", "cas": "947-19-3", "mw": 204, "cls": "aryl_ketone",
     "ions": [[99, 100], [105, 40], [77, 40], [81, 20], [55, 15], [204, 2]]},
    {"name": "2-Hydroxy-2-methylpropiophenone (Darocur 1173)", "cas": "7473-98-5", "mw": 164, "cls": "aryl_ketone",
     "ions": [[59, 100], [105, 50], [77, 40], [51, 10]]},
    {"name": "2,2-Dimethoxy-2-phenylacetophenone (Irgacure 651)", "cas": "24650-42-8", "mw": 256, "cls": "aryl_ketone",
     "ions": [[151, 100], [105, 20], [77, 25]]},
    {"name": "Methyl 2-benzoylbenzoate", "cas": "606-28-0", "mw": 240, "cls": "aryl_ketone",
     "ions": [[163, 100], [105, 40], [77, 40], [209, 15], [240, 3]]},
    {"name": "Acetophenone", "cas": "98-86-2", "mw": 120, "cls": "aryl_ketone",
     "ions": [[105, 100], [77, 80], [120, 35], [51, 30]]},
    {"name": "2-Isopropylthioxanthone (ITX)", "cas": "5495-84-1", "mw": 254, "cls": "thioxanthone",
     "ions": [[239, 100], [254, 60], [211, 15], [197, 15], [165, 10]]},
    {"name": "Ethyl 4-(dimethylamino)benzoate (EDB)", "cas": "10287-53-3", "mw": 193, "cls": "aminobenzoate",
     "ions": [[148, 100], [193, 60], [164, 40], [165, 20], [120, 10]]},
    {"name": "2-Ethylhexyl 4-(dimethylamino)benzoate (EHDAB)", "cas": "21245-02-3", "mw": 277, "cls": "aminobenzoate",
     "ions": [[165, 100], [148, 60], [277, 20], [164, 20]]},
    # monomers, oligomers, others
    {"name": "Styrene", "cas": "100-42-5", "mw": 104, "cls": "styrene",
     "ions": [[104, 100], [103, 45], [78, 40], [51, 20], [77, 15], [50, 10]]},
    {"name": "2,4-Diphenyl-1-butene (styrene dimer)", "cas": "16606-47-3", "mw": 208, "cls": "styrene",
     "ions": [[91, 100], [104, 70], [208, 20], [117, 15], [130, 10], [65, 10]]},
    {"name": "2,4,6-Triphenyl-1-hexene (styrene trimer)", "cas": "18964-53-9", "mw": 312, "cls": "styrene",
     "ions": [[91, 100], [117, 50], [104, 30], [207, 30], [194, 10], [312, 5]]},
    {"name": "ε-Caprolactam", "cas": "105-60-2", "mw": 113, "cls": "caprolactam",
     "ions": [[113, 100], [55, 80], [56, 60], [85, 60], [84, 50], [42, 40]]},
    {"name": "Bisphenol A", "cas": "80-05-7", "mw": 228, "cls": "bisphenol",
     "ions": [[213, 100], [228, 30], [119, 25], [214, 15], [91, 10]]},
    {"name": "4-tert-Octylphenol", "cas": "140-66-9", "mw": 206, "cls": "alkylphenol",
     "ions": [[135, 100], [107, 20], [136, 10], [206, 10], [41, 10]]},
    {"name": "Benzothiazole", "cas": "95-16-9", "mw": 135, "cls": "benzothiazole",
     "ions": [[135, 100], [108, 40], [69, 20], [63, 10], [82, 10]]},
    {"name": "2-(Methylthio)benzothiazole", "cas": "615-22-5", "mw": 181, "cls": "benzothiazole",
     "ions": [[181, 100], [148, 60], [136, 30], [108, 30]]},
    {"name": "2,4-Toluene diisocyanate (TDI)", "cas": "584-84-9", "mw": 174, "cls": "isocyanate",
     "ions": [[174, 100], [145, 40], [173, 30], [116, 10]]},
    {"name": "4,4'-Methylenediphenyl diisocyanate (MDI)", "cas": "101-68-8", "mw": 250, "cls": "isocyanate",
     "ions": [[250, 100], [221, 20], [249, 30], [165, 10]]},
    {"name": "4,4'-Methylenedianiline (MDA)", "cas": "101-77-9", "mw": 198, "cls": "aromatic_amine",
     "ions": [[198, 100], [197, 90], [106, 20], [182, 10], [180, 10]]},
    {"name": "2,4-Toluenediamine (TDA)", "cas": "95-80-7", "mw": 122, "cls": "aromatic_amine",
     "ions": [[122, 100], [121, 95], [94, 20], [104, 10]]},
    {"name": "Aniline", "cas": "62-53-3", "mw": 93, "cls": "aromatic_amine",
     "ions": [[93, 100], [66, 30], [65, 20], [39, 10]]},
    {"name": "Tributyl phosphate", "cas": "126-73-8", "mw": 266, "cls": "alkyl_phosphate",
     "ions": [[99, 100], [155, 20], [211, 10], [57, 10], [41, 15]]},
    {"name": "Triphenyl phosphate", "cas": "115-86-6", "mw": 326, "cls": "",
     "ions": [[326, 100], [325, 95], [77, 50], [215, 20], [170, 15], [65, 15]]},
    {"name": "2-Butoxyethanol", "cas": "111-76-2", "mw": 118, "cls": "glycol_ether",
     "ions": [[57, 100], [45, 60], [87, 20], [41, 30], [75, 10]]},
    {"name": "2-(2-Butoxyethoxy)ethanol", "cas": "112-34-5", "mw": 162, "cls": "glycol_ether",
     "ions": [[57, 100], [45, 80], [75, 40], [87, 20], [41, 30]]},
    {"name": "Methyl methacrylate", "cas": "80-62-6", "mw": 100, "cls": "",
     "ions": [[41, 100], [69, 90], [100, 60], [39, 40]]},
    {"name": "Butyl acrylate", "cas": "141-32-2", "mw": 128, "cls": "",
     "ions": [[55, 100], [73, 30], [56, 30], [41, 20], [85, 10]]},
    {"name": "2-Ethylhexyl acrylate", "cas": "103-11-7", "mw": 184, "cls": "",
     "ions": [[55, 100], [70, 50], [57, 45], [83, 25], [112, 20], [41, 30]]},
    {"name": "Benzaldehyde", "cas": "100-52-7", "mw": 106, "cls": "",
     "ions": [[105, 100], [106, 95], [77, 90], [51, 45]]},
    {"name": "Benzoic acid", "cas": "65-85-0", "mw": 122, "cls": "benzoate",
     "ions": [[105, 100], [122, 80], [77, 70], [51, 30]]},
    {"name": "Methyl benzoate", "cas": "93-58-3", "mw": 136, "cls": "benzoate",
     "ions": [[105, 100], [77, 60], [136, 30], [51, 25]]},
    {"name": "Benzyl alcohol", "cas": "100-51-6", "mw": 108, "cls": "",
     "ions": [[79, 100], [108, 90], [77, 60], [107, 60], [51, 20]]},
    {"name": "Phenol", "cas": "108-95-2", "mw": 94, "cls": "phenol",
     "ions": [[94, 100], [66, 30], [65, 25], [39, 15]]},
    {"name": "Cyclohexanone", "cas": "108-94-1", "mw": 98, "cls": "",
     "ions": [[55, 100], [42, 80], [98, 40], [69, 30], [70, 25]]},
    {"name": "Toluene", "cas": "108-88-3", "mw": 92, "cls": "alkylbenzene",
     "ions": [[91, 100], [92, 70], [65, 10], [39, 10]]},
    {"name": "Ethylbenzene", "cas": "100-41-4", "mw": 106, "cls": "alkylbenzene",
     "ions": [[91, 100], [106, 30], [51, 10], [65, 8], [77, 8]]},
    {"name": "Xylene (o/m/p)", "cas": "1330-20-7", "mw": 106, "cls": "alkylbenzene",
     "ions": [[91, 100], [106, 65], [105, 25], [77, 12], [51, 10]]},
    {"name": "Naphthalene", "cas": "91-20-3", "mw": 128, "cls": "pah",
     "ions": [[128, 100], [127, 10], [102, 8], [129, 10], [64, 5]]},
    {"name": "Biphenyl", "cas": "92-52-4", "mw": 154, "cls": "pah",
     "ions": [[154, 100], [153, 35], [152, 25], [76, 10]]},
    {"name": "Diphenyl ether", "cas": "101-84-8", "mw": 170, "cls": "",
     "ions": [[170, 100], [141, 40], [77, 35], [51, 30], [169, 10]]},
    {"name": "Limonene", "cas": "138-86-3", "mw": 136, "cls": "monoterpene",
     "ions": [[68, 100], [93, 80], [67, 70], [136, 20], [121, 20], [107, 20], [79, 30]]},
    {"name": "α-Pinene", "cas": "80-56-8", "mw": 136, "cls": "monoterpene",
     "ions": [[93, 100], [92, 35], [91, 40], [77, 30], [121, 10], [136, 8]]},
    {"name": "Squalene", "cas": "111-02-4", "mw": 410, "cls": "squalene",
     "ions": [[69, 100], [81, 60], [41, 50], [95, 25], [121, 10], [137, 10]]},
    {"name": "Hexamethylcyclotrisiloxane (D3)", "cas": "541-05-9", "mw": 222, "cls": "siloxane_cyclic",
     "ions": [[207, 100], [96, 10], [191, 10], [208, 20], [209, 10], [133, 5]]},
    {"name": "Octamethylcyclotetrasiloxane (D4)", "cas": "556-67-2", "mw": 296, "cls": "siloxane_cyclic",
     "ions": [[281, 100], [73, 20], [265, 15], [282, 25], [283, 15], [249, 10], [133, 10]]},
    {"name": "Decamethylcyclopentasiloxane (D5)", "cas": "541-02-6", "mw": 370, "cls": "siloxane_cyclic",
     "ions": [[355, 100], [73, 40], [267, 30], [251, 20], [356, 30], [357, 20]]},
    {"name": "Dodecamethylcyclohexasiloxane (D6)", "cas": "540-97-6", "mw": 444, "cls": "siloxane_cyclic",
     "ions": [[73, 100], [341, 60], [429, 40], [147, 30], [327, 20], [355, 10]]},
]


def _valid_condition(c) -> bool:
    keys = {"ion", "any", "base", "top", "ratio", "series", "m", "parity", "mrel", "loss", "iso"}
    return isinstance(c, dict) and len(keys & set(c)) == 1


def validate_rule(r: dict) -> list[str]:
    errs = []
    for k in ("id", "label"):
        if not isinstance(r.get(k), str) or not r.get(k):
            errs.append(f"rule without {k}")
    if not r.get("require"):
        errs.append(f"rule {r.get('id')}: no 'require' conditions")
    for part in ("require", "support", "contra"):
        for c in r.get(part, []) or []:
            if not _valid_condition(c):
                errs.append(f"rule {r.get('id')}: invalid condition {c!r}")
            if "series" in c and c["series"] not in SERIES:
                errs.append(f"rule {r.get('id')}: unknown series {c['series']!r}")
    return errs


def validate_fingerprint(f: dict) -> list[str]:
    errs = []
    if not f.get("name") or not isinstance(f.get("ions"), list) or not f["ions"]:
        errs.append(f"fingerprint {f.get('name')!r}: needs name and ions")
    for p in f.get("ions") or []:
        if not (isinstance(p, (list, tuple)) and len(p) == 2):
            errs.append(f"fingerprint {f.get('name')!r}: invalid ion {p!r}")
    return errs


_CACHE: dict = {}


def load(extra_path: Path | None = None) -> tuple[list[dict], list[dict]]:
    """Built-in rules and fingerprints plus the valid entries of the user file."""
    if extra_path is None:
        from gcws import paths
        extra_path = paths.DATA / "interpret_rules.json"
    try:
        stamp = extra_path.stat().st_mtime if extra_path.exists() else None
    except OSError:
        stamp = None
    key = (str(extra_path), stamp)
    if key in _CACHE:
        return _CACHE[key]
    rules, fps = list(RULES), list(FINGERPRINTS)
    if stamp is not None:
        try:
            data = json.loads(extra_path.read_text(encoding="utf-8"))
            for r in data.get("rules", []):
                errs = validate_rule(r)
                if errs:
                    log.warning("interpret_rules.json: %s", "; ".join(errs))
                    continue
                rules = [x for x in rules if x["id"] != r["id"]] + [r]
            for f in data.get("fingerprints", []):
                errs = validate_fingerprint(f)
                if errs:
                    log.warning("interpret_rules.json: %s", "; ".join(errs))
                    continue
                fps.append(f)
        except (OSError, ValueError, AttributeError) as exc:
            log.warning("interpret_rules.json not readable: %s", exc)
    _CACHE.clear()
    _CACHE[key] = (rules, fps)
    return rules, fps
