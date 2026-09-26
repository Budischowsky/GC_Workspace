"""Search parameters of ``/api/analyze`` beyond the scan range: which libraries
in which order, per-library minimum scores, the match algorithm and the hit
constraints (molecular weight, elements, name, CAS) that commercial GC/MS
search programs offer (Agilent MassHunter "Library Search Parameters",
Shimadzu GCMSsolution "Search Conditions", NIST MS Search "Constraints").

Everything is optional; an old client that sends only ``libraries``,
``min_mz``, ``max_mz`` and ``threshold`` gets exactly the previous search.
"""
from dataclasses import dataclass, field
import math
import re

ALGORITHMS = ('pbm', 'similarity')
MODES = ('combined', 'sequential')
DEFAULT_MAX_HITS = 20
MAX_HITS = 50

ELEMENT = re.compile(r'([A-Z][a-z]?)(\d*)')


def formula_elements(formula):
    """``{"C": 7, "H": 8}`` for a plain molecular formula; ``{}`` when unreadable.

    Unlike ``chemistry.formula_info`` any element symbol is accepted, so a
    constraint can exclude metals the fragment chemistry does not know.
    """
    text = re.sub(r'\s+', '', str(formula or ''))
    tokens = ELEMENT.findall(text)
    if not tokens or ''.join(e + n for e, n in tokens) != text:
        return {}
    atoms = {}
    for element, count in tokens:
        atoms[element] = atoms.get(element, 0) + int(count or 1)
    return atoms


def _elements(value, label):
    if value in (None, '', []):
        return frozenset()
    items = value if isinstance(value, list) else re.split(r'[\s,;]+', str(value))
    symbols = frozenset(s.strip() for s in items if str(s).strip())
    bad = [s for s in symbols if not re.fullmatch(r'[A-Z][a-z]?', s)]
    if bad:
        raise ValueError(f'{label}: invalid element symbol(s) ' + ', '.join(sorted(bad)) + '.')
    return symbols


def _words(value, label):
    if value in (None, '', []):
        return ()
    items = value if isinstance(value, list) else str(value).split(';')
    if not all(isinstance(s, str) for s in items):
        raise ValueError(f'{label}: expected text.')
    return tuple(s.strip().casefold() for s in items if s.strip())


def _number(value, label, low, high, integer=False):
    if value in (None, ''):
        return None
    try:
        number = float(value)
    except (TypeError, ValueError):
        raise ValueError(f'{label}: expected a number.') from None
    if not math.isfinite(number) or not low <= number <= high:
        raise ValueError(f'{label}: must be between {low} and {high}.')
    return int(round(number)) if integer else number


def _mw(meta):
    try:
        value = float(meta.get('mw') or 0)
    except (TypeError, ValueError):
        value = 0.0
    if value > 0:
        return value
    from chemistry import formula_info
    return formula_info(meta.get('formula', '')).get('nominal_mass')


@dataclass
class Constraints:
    mw_min: float = None
    mw_max: float = None
    elements_required: frozenset = frozenset()
    elements_allowed: frozenset = frozenset()
    name_include: tuple = ()
    name_exclude: tuple = ()
    require_cas: bool = False

    @property
    def active(self):
        return bool(self.mw_min is not None or self.mw_max is not None or self.elements_required
                    or self.elements_allowed or self.name_include or self.name_exclude or self.require_cas)

    def accepts(self, meta):
        """True when a reference's metadata satisfies every constraint."""
        if self.require_cas and not str(meta.get('cas') or '').strip('0- '):
            return False
        if self.mw_min is not None or self.mw_max is not None:
            mw = _mw(meta)
            if mw is None or (self.mw_min is not None and mw < self.mw_min) \
                    or (self.mw_max is not None and mw > self.mw_max):
                return False
        if self.elements_required or self.elements_allowed:
            atoms = formula_elements(meta.get('formula', ''))
            if not atoms:
                return False  # an unknown formula cannot satisfy an element constraint
            if not self.elements_required <= set(atoms):
                return False
            if self.elements_allowed and not set(atoms) <= self.elements_allowed:
                return False
        if self.name_include or self.name_exclude:
            name = str(meta.get('name') or '').casefold()
            if self.name_include and not any(w in name for w in self.name_include):
                return False
            if any(w in name for w in self.name_exclude):
                return False
        return True

    def as_dict(self):
        return dict(mw_min=self.mw_min, mw_max=self.mw_max,
                    elements_required=sorted(self.elements_required),
                    elements_allowed=sorted(self.elements_allowed),
                    name_include=list(self.name_include), name_exclude=list(self.name_exclude),
                    require_cas=self.require_cas)


@dataclass
class SearchOptions:
    algorithm: str = 'pbm'
    mode: str = 'combined'
    stop_score: int = 80
    library_min_scores: dict = field(default_factory=dict)
    constraints: Constraints = field(default_factory=Constraints)
    max_hits: int = DEFAULT_MAX_HITS
    dedupe: bool = True
    lite: bool = False

    def as_dict(self):
        return dict(algorithm=self.algorithm, mode=self.mode, stop_score=self.stop_score,
                    library_min_scores=dict(self.library_min_scores), max_hits=self.max_hits,
                    dedupe=self.dedupe, lite=self.lite, constraints=self.constraints.as_dict())


def parse(settings, libraries):
    """Validated :class:`SearchOptions` from the request ``settings``."""
    algorithm = settings.get('algorithm') or 'pbm'
    if algorithm not in ALGORITHMS:
        raise ValueError('Unknown search algorithm; use "pbm" or "similarity".')
    mode = settings.get('mode') or 'combined'
    if mode not in MODES:
        raise ValueError('Unknown search mode; use "combined" or "sequential".')
    stop = _number(settings.get('stop_score'), 'Stop score', 0, 99, integer=True)
    scores = settings.get('library_min_scores') or {}
    if not isinstance(scores, dict):
        raise ValueError('library_min_scores must map library names to scores.')
    unknown = set(scores) - set(libraries)
    minimums = {}
    for name, value in scores.items():
        if name in unknown:
            continue  # a minimum for a library that is not searched is irrelevant
        minimums[name] = _number(value, f'Minimum score of {name}', 0, 99, integer=True) or 0
    constraints = Constraints(
        mw_min=_number(settings.get('mw_min'), 'Minimum MW', 0, 10000),
        mw_max=_number(settings.get('mw_max'), 'Maximum MW', 0, 10000),
        elements_required=_elements(settings.get('elements_required'), 'Required elements'),
        elements_allowed=_elements(settings.get('elements_allowed'), 'Allowed elements'),
        name_include=_words(settings.get('name_include'), 'Name contains'),
        name_exclude=_words(settings.get('name_exclude'), 'Name excludes'),
        require_cas=bool(settings.get('require_cas', False)))
    if constraints.mw_min is not None and constraints.mw_max is not None \
            and constraints.mw_min > constraints.mw_max:
        raise ValueError('Minimum MW is larger than maximum MW.')
    if constraints.elements_allowed and not constraints.elements_required <= constraints.elements_allowed:
        raise ValueError('A required element is not among the allowed elements.')
    max_hits = _number(settings.get('max_hits'), 'Hits', 1, MAX_HITS, integer=True) or DEFAULT_MAX_HITS
    return SearchOptions(algorithm=algorithm, mode=mode, stop_score=80 if stop is None else stop,
                         library_min_scores=minimums, constraints=constraints, max_hits=max_hits,
                         dedupe=bool(settings.get('dedupe', True)), lite=bool(settings.get('lite', False)))


def similarity_scores(forward_cosine, reverse_cosine):
    """NIST-style match factors (0-999) from the weighted cosines, and a 0-99 score."""
    mf = int(round(999 * max(0.0, min(1.0, forward_cosine)) ** 2))
    rmf = int(round(999 * max(0.0, min(1.0, reverse_cosine)) ** 2))
    return mf, rmf, min(99, int(round(mf / 10)))
