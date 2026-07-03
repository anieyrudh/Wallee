from wallee.predicates import PredicateEvaluator, all_of, any_of, atom, negate


def test_predicates_fail_closed_on_missing_fact():
    evaluator = PredicateEvaluator()
    assert evaluator.evaluate(atom("missing", "==", 1), {}) is False


def test_nested_predicates():
    evaluator = PredicateEvaluator()
    facts = {"a": 1, "b": 2, "c": True}
    predicate = all_of(atom("a", "==", 1), any_of(atom("b", ">", 1), negate(atom("c", "==", True))))
    assert evaluator.evaluate(predicate, facts) is True
