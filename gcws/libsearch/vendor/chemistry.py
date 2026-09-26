"""Conservative nominal-mass EI evidence; hypotheses, never unique assignments."""
import math
import re

ATOMS = {'C': 12, 'H': 1, 'D': 2, 'N': 14, 'O': 16, 'F': 19, 'Si': 28,
         'P': 31, 'S': 32, 'Cl': 35, 'Br': 79, 'I': 127, 'B': 11}


def formula_info(formula):
    tokens = re.findall(r'([A-Z][a-z]?)(\d*)', formula or '')
    if not tokens or ''.join(e + n for e, n in tokens) != formula or any(e not in ATOMS for e, _ in tokens):
        return {'nominal_mass': None, 'dbe': None, 'elements': {}}
    atoms = {}
    for element, count in tokens:
        atoms[element] = atoms.get(element, 0) + int(count or 1)
    nominal = sum(ATOMS[e] * n for e, n in atoms.items())
    # Standard DBE is not generally valid for organometallics or hypervalent P/S.
    dbe = None
    if set(atoms) <= {'C', 'H', 'D', 'N', 'O', 'F', 'Cl', 'Br', 'I'}:
        dbe = 1 + atoms.get('C', 0) + (atoms.get('N', 0) - sum(atoms.get(e, 0) for e in ('H', 'D', 'F', 'Cl', 'Br', 'I'))) / 2
    return {'nominal_mass': nominal, 'dbe': dbe, 'elements': atoms}


def family_tags(meta):
    name = meta.get('name', '').lower()
    atoms = formula_info(meta.get('formula', ''))['elements']
    tags = []
    for pattern, tag in [
        (r'phenol', 'Phenols'), (r'phosphite', 'Phosphites'), (r'phosphate', 'Phosphates'),
        (r'siloxane|silane|silyl', 'Organosilicon compounds'), (r'phthal', 'Phthalates'),
        (r'benz|phenyl|phenol|toluene|xylene|naphth|anthrac|styrene', 'Aromatic compounds'),
        (r'amine|amino', 'Amines'), (r'nitrile|cyanide', 'Nitriles'),
        (r'ketone|acetone|[a-z]anone|acetophenone', 'Ketones'),
        (r'aldehyde|[a-z]anal\b', 'Aldehydes'),
        (r'acid,.*ester|acetate|propionate|benzoate', 'Esters'),
        (r'ethanol|methanol|propanol|butanol|pentanol|hexanol|heptanol|octanol|alcohol', 'Alcohols'),
        (r'tert-butyl|dimethylethyl', 'tert-Butyl substitution'),
    ]:
        if re.search(pattern, name):
            tags.append(tag)
    for atom, tag in [('Cl', 'Chlorinated compounds'), ('Br', 'Brominated compounds'),
                      ('P', 'Organophosphorus compounds'), ('Si', 'Organosilicon compounds')]:
        if atom in atoms and tag not in tags:
            tags.append(tag)
    if atoms and set(atoms) <= {'C', 'H'}:
        tags.append('Hydrocarbons')
    return tags


FRAGMENTS = {
    31: ('CH3O+ / CH2OH+', 'Oxygen-stabilized ions from alpha cleavage; compatible with some alcohols and ethers.'),
    43: ('C3H7+ or C2H3O+', 'Alkyl cation or acylium ion; nominal mass cannot distinguish them.'),
    57: ('C4H9+ or C3H5O+', 'Often alkyl cleavage, including tert-butyl groups; an oxygenated ion is also possible.'),
    60: ('C2H4O2+• candidate', 'A possible McLafferty rearrangement ion in suitable carboxylic acids; requires structural context.'),
    74: ('C3H6O2+• candidate', 'A possible McLafferty rearrangement ion in suitable methyl esters; not exclusive to that family.'),
    77: ('C6H5+ candidate', 'Compatible with an aromatic fragment; other nominal formulas remain possible.'),
    91: ('C7H7+ candidate', 'Benzyl/tropylium-type ion, often associated with alkyl-substituted aromatic compounds.'),
    105: ('C7H5O+ or C8H9+', 'Benzoyl-type acylium or alkyl-aromatic ion; mass alone does not resolve the formula.'),
    149: ('C8H5O3+ candidate', 'Common phthalate-associated ion; require companion ions and a library match.'),
    73: ('C3H9Si+ candidate', 'Trimethylsilyl-type ion; also possible oxygen-containing ions at this nominal mass.'),
    147: ('C5H15OSi2+ candidate', 'Can support a siloxane/TMS pattern when other characteristic ions agree.'),
}

LOSSES = {15: ('CH3•', 'methyl radical cleavage'), 18: ('H2O', 'possible dehydration'),
          28: ('CO / C2H4', 'isobaric neutral-loss alternatives'), 29: ('C2H5• / CHO•', 'radical-loss alternatives'),
          31: ('CH3O•', 'possible methoxy radical loss'), 32: ('CH3OH', 'possible methanol elimination'),
          44: ('CO2 / C2H4O', 'isobaric neutral-loss alternatives'),
          56: ('C4H8', 'possible butene elimination'), 57: ('C4H9•', 'possible butyl radical cleavage')}

ION_FORMULAS = {31: ['CH3O'], 43: ['C3H7', 'C2H3O'], 57: ['C4H9', 'C3H5O'],
                60: ['C2H4O2'], 74: ['C3H6O2'], 77: ['C6H5'], 91: ['C7H7'],
                105: ['C7H5O', 'C8H9'], 149: ['C8H5O3'], 73: ['C3H9Si'], 147: ['C5H15OSi2']}
LOSS_FORMULAS = {15: ['CH3'], 18: ['H2O'], 28: ['CO', 'C2H4'], 29: ['C2H5', 'CHO'],
                 31: ['CH3O'], 32: ['CH4O'], 44: ['CO2', 'C2H4O'], 56: ['C4H8'], 57: ['C4H9']}

# Nominal-loss examples, not an exhaustive formula or pathway assignment.
# Alkyl radical examples: OpenStax Organic Chemistry, section 12.2.
SPACING_CONTEXT = {
    15: 'Possible loss of a methyl radical (CH3•) by cleavage of a methyl-bearing group. The position of that group is not established.',
    18: 'Possible loss of water (H2O), for example dehydration in a suitable oxygen-containing structure. Hydrogen transfer may be required.',
    28: 'Possible loss of carbon monoxide (CO) from a suitable carbonyl-containing ion, or ethene (C2H4). Nominal mass cannot distinguish these alternatives.',
    29: 'Possible loss of an ethyl radical (C2H5•) or formyl radical (CHO•). The local alkyl or carbonyl structure must support the proposed cleavage.',
    31: 'Possible loss of a methoxy radical (CH3O•) from a suitable methoxy-containing structure. A methoxy group is not established by the spacing alone.',
    32: 'Possible elimination of methanol (CH3OH) from a suitable oxygen-containing structure, potentially involving hydrogen transfer.',
    44: 'Possible loss of carbon dioxide (CO2) or a neutral with formula C2H4O. Nominal mass alone does not identify the neutral or its structure.',
    56: 'Possible elimination of butene (C4H8), including isobutene from a suitable tert-butyl-containing structure with hydrogen transfer. The butene isomer is unassigned.',
    57: 'Possible loss of a butyl radical (C4H9•), including tert-butyl if the structure supports it. The spacing cannot distinguish butyl isomers; a peak at m/z 57 is a separate ion observation.',
}


def fits_elements(formula, elements):
    return all(n <= elements.get(e, 0) for e, n in formula_info(formula)['elements'].items())


def residual_formula(elements, loss):
    removed = formula_info(loss)['elements']
    remaining = {e: n - removed.get(e, 0) for e, n in elements.items()}
    order = [e for e in ('C', 'H') if e in remaining] + sorted(set(remaining) - {'C', 'H'})
    return ''.join(e + (str(remaining[e]) if remaining[e] != 1 else '') for e in order if remaining[e] > 0)


def observed_spacings(query):
    """Observed endpoints only; EI MS1 does not establish precursor/product links."""
    masses = sorted(sorted((m for m in query if query[m] >= 2), key=query.get, reverse=True)[:30])
    pairs = []
    for parent in masses:
        for product in masses:
            delta = parent - product
            if delta in LOSSES:
                neutral, mechanism = LOSSES[delta]
                pairs.append(dict(parent=parent, product=product, loss=delta, neutral=neutral,
                                  parent_intensity=round(query[parent], 2), product_intensity=round(query[product], 2),
                                  explanation=mechanism))
    pairs.sort(key=lambda p: (-min(p['parent_intensity'], p['product_intensity']), p['parent'], p['product']))
    return pairs[:20]


def interpret(query, hits, minimum, maximum):
    fragments = [dict(mz=m, intensity=round(query[m], 2), assignment=a, explanation=e)
                 for m, (a, e) in FRAGMENTS.items() if query.get(m, 0) >= 2]
    fragments.sort(key=lambda x: -x['intensity'])
    isotope = []
    for m, intensity in query.items():
        if intensity < 10 or m + 2 > maximum or query.get(m - 2, 0) > intensity * 0.5:
            continue
        ratio = query.get(m + 2, 0) / intensity
        kind = 'one Cl atom' if 0.24 <= ratio <= 0.42 else 'one Br atom' if 0.75 <= ratio <= 1.25 else None
        if kind:
            isotope.append(dict(mz=m, ratio=round(ratio, 3), hypothesis=kind,
                                explanation='A two-dalton pair is compatible with this isotope pattern. Overlapping fragments can mimic it; this need not be the molecular ion.'))
    isotope.sort(key=lambda x: -query[x['mz']])
    clues = []
    def clue(label, masses, text, required=2, floor=2):
        present = [m for m in masses if query.get(m, 0) >= floor]
        if len(present) >= required:
            clues.append(dict(family=label, ions=present, explanation=text))
    clue('Alkyl-substituted aromatic motif', [77, 91, 105], 'Companion aromatic-type ions support an aromatic motif; they do not establish a ring substitution pattern.')
    clue('Aliphatic chains / alkyl fragments', [43, 57, 71, 85], 'A 14 Da alkyl-ion series supports hydrocarbon-chain fragmentation; these ions occur in many functional groups.')
    clue('Possible siloxane or silylated material', [73, 147, 207, 281], 'Companion silicon-associated ions can arise from derivatization, silicone contamination, or column bleed. Nominal isobars remain possible.', 3, floor=5)
    clue('Possible phthalate motif', [149, 104, 167], 'A phthalate-like fragment set is a screening clue; inspect the reference comparison.', 2)
    if query.get(57, 0) >= 10 and query.get(191, 0) >= 10 and query.get(206, 0) >= 1:
        clues.append(dict(family='Possible di-tert-butylphenol motif', ions=[57, 191, 206],
                          explanation='The 206 → 191 spacing is compatible with loss of CH3•, accompanied by a strong nominal C4H9+ ion. Larger molecules containing this motif can produce the same ions.'))
    consensus = {}
    eligible = [h for h in hits[:10] if h['score'] >= max(60, hits[0]['score'] - 5)] if hits else []
    for hit in eligible:
        for tag in hit['families']:
            consensus[tag] = consensus.get(tag, 0) + 1
    families = [dict(family=k, count=v, total=len(eligible), basis='Name/formula tags among close library candidates; not a statistical probability.')
                for k, v in sorted(consensus.items(), key=lambda x: -x[1])[:6]]
    warnings = []
    if not hits or hits[0]['score'] < 60:
        warnings.append('No persuasive identity match under these settings. Use fragment motifs as leads and expand the reference library.')
    if hits and hits[0]['reverse'] - hits[0]['forward'] > 12:
        warnings.append('The reverse PBM match is substantially better than the forward match. Extra query ions may reflect background, coelution, or an incorrect candidate; a single MSP spectrum cannot resolve these possibilities.')
    if len(hits) > 1 and hits[0]['score'] - hits[1]['score'] < 3:
        warnings.append('The leading distinct candidates are closely scored. Isomers or related compounds may remain unresolved.')
    if len(query) < 6:
        warnings.append('Very few retained peaks: high similarities can be accidental.')
    if len(isotope) > 3:
        warnings.append('Several apparent isotope pairs occur in this dense spectrum; coincidences between unrelated fragments are plausible.')
    return dict(fragments=fragments, isotope_pairs=isotope[:6], motifs=clues, families=families, warnings=warnings,
                observed_spacings=observed_spacings(query))


def candidate_evidence(meta, query, reference, minimum, maximum):
    info = formula_info(meta.get('formula', ''))
    mass = info['nominal_mass']
    losses = []
    if mass is not None:
        for delta, (neutral, mechanism) in LOSSES.items():
            product = mass - delta
            compatible = [f for f in LOSS_FORMULAS[delta] if fits_elements(f, info['elements'])]
            if query.get(product, 0) >= 1 and compatible:
                losses.append(dict(parent=mass, product=product, loss=delta, neutral=neutral,
                                   intensity=round(query[product], 2), explanation=mechanism,
                                   compatible_formulas=compatible, parent_observed=query.get(mass, 0) >= .1))
    strongest = sorted(query, key=query.get, reverse=True)
    shared = [dict(mz=m, query=round(query[m], 3), reference=round(reference[m], 3))
              for m in strongest if m in reference][:12]
    unmatched = [dict(mz=m, intensity=round(query[m], 3)) for m in strongest if m not in reference][:10]
    missing = [dict(mz=m, intensity=round(reference[m], 3)) for m in sorted(reference, key=reference.get, reverse=True)
               if m not in query][:10]
    parent_status = 'Formula unavailable or unsupported'
    if mass is not None:
        parent_status = 'Outside selected scan range' if not minimum <= mass <= maximum else (
            'Observed at candidate nominal M (assignment unconfirmed)' if query.get(mass, 0) >= 0.1 else 'Not observed at candidate nominal M; EI molecular ions can be weak or absent')
    fragment_details = []
    spacings = observed_spacings(query)
    for m in strongest[:20]:
        known = ION_FORMULAS.get(m, [])
        compatible = [f for f in known if fits_elements(f, info['elements'])] if info['elements'] else []
        context = []
        residuals = []
        if m == mass:
            context.append('At candidate nominal M; possible molecular ion, assignment unconfirmed.')
            residuals.append(meta['formula'])
        for loss in losses:
            if loss['product'] == m:
                residuals.extend(residual_formula(info['elements'], f) for f in loss['compatible_formulas'])
                context.append(f"Candidate M minus {loss['loss']} Da; residual formulas assume that loss and this candidate formula.")
        if query.get(m - 1, 0) >= 10 and .01 <= query[m] / query[m - 1] <= .5:
            context.append(f"One mass unit above m/z {m-1}: {100*query[m]/query[m-1]:.1f}% of that ion. Possible isotope satellite or independent fragment; no isotope assignment is established.")
        links = [p for p in spacings if m in (p['parent'], p['product'])][:3]
        spacing_details = []
        for link in links:
            compatible_losses = [f for f in LOSS_FORMULAS[link['loss']]
                                 if fits_elements(f, info['elements'])] if info['elements'] else []
            check = ('Candidate formula unavailable; element compatibility cannot be checked.' if not info['elements'] else
                     'Catalog loss formulas exceed candidate element counts.' if not compatible_losses else
                     'Fits candidate element counts: ' + ' / '.join(compatible_losses) + '. Higher-mass ion formula and pathway remain unassigned.')
            spacing_details.append(dict(**link, interpretation=SPACING_CONTEXT[link['loss']],
                                        compatible_formulas=compatible_losses, element_check=check))
        if links:
            context.append('Observed spacings: ' + '; '.join(f"{p['parent']} → {p['product']} ({p['loss']} Da)" for p in links) + '. Connections are unconfirmed.')
        fragment_details.append(dict(mz=m, query=round(query[m], 2), reference=round(reference.get(m, 0), 2),
                                     shared=m in reference, catalog_formulas=known,
                                     compatible_formulas=compatible,
                                     loss_constrained_formulas=list(dict.fromkeys(residuals)), context=context,
                                     observed_spacings=spacing_details,
                                     element_check=('Candidate formula unavailable' if not info['elements'] else
                                                    'No catalog formula for this ion' if not known else
                                                    'Catalog formulas exceed candidate element counts' if not compatible else
                                                    'Element counts fit; structure and charge are unassigned'),
                                     explanation=FRAGMENTS.get(m, ('', 'No specific rule in the small ion catalog; inspect companion ions and the reference.'))[1]))
    envelope = []
    if mass and minimum <= mass <= maximum:
        for delta in (0, 1, 2):
            if mass + delta <= maximum:
                envelope.append(dict(mz=mass + delta, label='M' if delta == 0 else f'M+{delta}',
                                     query=round(query.get(mass + delta, 0), 3), reference=round(reference.get(mass + delta, 0), 3)))
    return dict(**info, molecular_ion=parent_status, molecular_intensity=round(query.get(mass, 0), 3),
                neutral_losses=losses, shared=shared, unmatched=unmatched, missing=missing,
                fragment_details=fragment_details, molecular_envelope=envelope,
                nitrogen_rule=('For a neutral closed-shell organic molecule, an odd nominal molecular mass is consistent with an odd N count; apply only if M is correctly assigned.' if mass else None))
