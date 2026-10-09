"""gcws.learn.folds: locked test batches and cross-validation folds, grouped by batch."""
import random


def test_split_is_deterministic_and_disjoint():
    from gcws.learn.folds import make_split
    batches = [f"batch_{i:02d}" for i in range(13)]
    s = make_split(batches)
    assert len(s.test) == 3
    rest = [b for fold in s.folds for b in fold]
    assert len(s.folds) == 5 and sorted(rest) == sorted(set(batches) - set(s.test))
    assert not set(rest) & set(s.test)
    shuffled = batches[:]
    random.Random(1).shuffle(shuffled)
    assert make_split(shuffled) == s


def test_few_batches():
    from gcws.learn.folds import make_split
    s = make_split(["a", "b", "c"])
    assert len(s.test) == 1 and len(s.folds) == 2 and all(len(f) == 1 for f in s.folds)


def test_one_batch():
    from gcws.learn.folds import make_split
    s = make_split(["only"])
    assert s.test == [] and s.folds == [["only"]]
