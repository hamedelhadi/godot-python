from typing import Optional, List, Dict, Set, Sequence, Tuple, Any
from pathlib import Path
from hashlib import sha256
from pickle import dumps as pickle_dumps

from ._rule import ResolvedRule
from ._const import ConstTypes
from ._exceptions import IsengardRunError, IsengardUnknownTargetError, IsengardConsistencyError
from ._target import TargetHandlersBundle, BaseTargetHandler, ResolvedTargetID
from ._db import DB


class Runner:
    def __init__(
        self,
        rules: Dict[str, ResolvedRule],
        config: Dict[str, ConstTypes],
        target_handlers: TargetHandlersBundle,
        db_path: Path,
    ):
        self.target_to_rule = {
            output: rule for rule in rules.values() for output in rule.resolved_outputs
        }
        self.rules = rules
        self.config = config
        self.target_handlers = target_handlers
        self.db_path = db_path

    def _compute_run_fingerprint(self, rule) -> bytes:
        # Run fingerprint is computed from the config and the rule's ID.
        # Hence we don't check if the rule's code itself has changed (the
        # user should declare the script file as input of the rule if this
        # check in needed)
        h = sha256(rule.id.encode("utf8"))
        for k in sorted(rule.needed_config):
            h.update(pickle_dumps(self.config[k]))
        return h.digest()

    def clean(self, target: ResolvedTargetID) -> None:
        try:
            rule = self.target_to_rule[target]
        except KeyError:
            raise IsengardUnknownTargetError(f"No rule has target `{target}` as output")

        already_cleaned: Set[ResolvedRule] = set()

        def _clean(rule: ResolvedRule, parent_rules: Sequence[ResolvedRule]) -> None:
            if rule in already_cleaned:
                return
            already_cleaned.add(rule)

            run_fingerprint = self._compute_run_fingerprint(rule)
            targets_previous_fingerprint = db.fetch_rule_previous_run(run_fingerprint) or {}
            for target in rule.resolved_outputs:
                try:
                    previous_fingerprint = targets_previous_fingerprint[target]
                    cooked, handler = self.target_handlers.cook_target(target, previous_fingerprint)
                except KeyError:
                    cooked, handler = self.target_handlers.cook_target(target, None)
                try:
                    handler.clean(cooked)
                except Exception as exc:
                    raise IsengardRunError(f"Error while cleaning rule `{rule.id}`: {exc}") from exc

            for input_target in rule.resolved_inputs:
                sub_parent_rules = [*parent_rules, rule]

                try:
                    sub_rule = self.target_to_rule[input_target]

                except KeyError:
                    # Input has not been generated by a rule, it is most likely
                    # because it is a source file on disk
                    input_handler = self.target_handlers.get_handler(input_target)
                    if not input_handler.ON_DISK_TARGET:
                        raise IsengardUnknownTargetError(
                            f"No rule has target `{input_target}` as output (needed by {' -> '.join(r.id for r in sub_parent_rules)}"
                        )

                else:
                    if sub_rule in sub_parent_rules:
                        raise IsengardConsistencyError(
                            f"Recursion detection in rules {' -> '.join(r.id for r in sub_parent_rules)}"
                        )
                    _clean(sub_rule, sub_parent_rules)

        with DB.connect(self.db_path) as db:
            _clean(rule, [])

    def run(self, target: ResolvedTargetID) -> bool:
        try:
            rule = self.target_to_rule[target]
        except KeyError:
            raise IsengardUnknownTargetError(f"No rule has target `{target}` as output")

        return self.run_rule(rule)

    def run_rule(self, rule: ResolvedRule) -> bool:
        # {<rule>: <has_been_rebuilt>}
        already_evaluated: Dict[ResolvedRule, bool] = {}
        # {<target>: (<cooked>, <handler>, <has_changed>)}
        targets_eval_cache: Dict[ResolvedTargetID, Tuple[Any, BaseTargetHandler, bool]] = {}

        def _run(rule: ResolvedRule) -> bool:
            # 0) Fast track if the rule has already been evaluated
            try:
                return already_evaluated[rule]
            except KeyError:
                pass

            to_cache_targets: List[ResolvedTargetID] = []

            # 1) Retreive previous run from DB
            run_fingerprint = self._compute_run_fingerprint(rule)
            targets_previous_fingerprint = db.fetch_rule_previous_run(run_fingerprint)
            if targets_previous_fingerprint is None:
                # If we end up here, two possibilities:
                # - rule have never run
                # - rule have already run, but with a different config
                # In both case the rebuild is obviously needed !
                rebuild_needed = True
                targets_previous_fingerprint = {}
            else:
                rebuild_needed = False

            # 2) Evaluate each input
            for input_target in rule.resolved_inputs:
                try:
                    subrule = self.target_to_rule[input_target]
                except KeyError:
                    # Input has not been generated by a rule, it is most likely
                    # because it is a source file on disk
                    try:
                        input_previous_fingerprint = targets_previous_fingerprint[input_target]
                    except KeyError:
                        input_previous_fingerprint = None
                    input_cooked, input_handler = self.target_handlers.cook_target(
                        input_target,
                        input_previous_fingerprint,
                    )

                    if not input_handler.ON_DISK_TARGET:
                        raise IsengardUnknownTargetError(
                            f"No rule has target `{input_target!r}` as output (needed by rule `{rule.id}`)"
                        )
                    else:
                        # The target must be a prerequisit existing on disk
                        if input_previous_fingerprint is not None:
                            input_has_changed = input_handler.need_rebuild(
                                input_cooked, input_previous_fingerprint
                            )
                        else:
                            input_has_changed = True
                        targets_eval_cache[input_target] = (
                            input_cooked,
                            input_handler,
                            input_has_changed,
                        )
                        rebuild_needed |= input_has_changed
                        # Given this target is not generated by any rule, we add it to the
                        # rule that depend on it
                        to_cache_targets.append(input_target)
                else:
                    # Go recursive on the rule we depend on
                    rebuild_needed |= _run(subrule)

            # 3) Evaluate the outputs
            outputs_cooked: List[Tuple[ResolvedTargetID, Any, BaseTargetHandler]] = []
            for output_target in rule.resolved_outputs:
                try:
                    output_previous_fingerprint = targets_previous_fingerprint[output_target]
                except KeyError:
                    output_previous_fingerprint = None
                output_cooked, output_handler = self.target_handlers.cook_target(
                    output_target,
                    output_previous_fingerprint,
                )
                if output_previous_fingerprint is not None:
                    output_has_changed = output_handler.need_rebuild(
                        output_cooked, output_previous_fingerprint
                    )
                else:
                    output_has_changed = True
                outputs_cooked.append(output_cooked)
                targets_eval_cache[output_target] = (
                    output_cooked,
                    output_handler,
                    output_has_changed,
                )
                to_cache_targets.append(output_target)
                rebuild_needed |= output_has_changed

            already_evaluated[rule] = rebuild_needed

            # 5) Actually run the rule if needed
            if not rebuild_needed:
                return False
            print(f"> {rule.id}")
            inputs_cooked = [targets_eval_cache[t][0] for t in rule.resolved_inputs]
            try:
                rule.run(outputs_cooked, inputs_cooked, self.config)
            except Exception as exc:
                raise IsengardRunError(f"Error in rule `{rule.id}`: {exc}") from exc

            # 6) Update the build cache
            target_fingerprints = []
            for target in to_cache_targets:
                target_cooked, target_handler, _ = targets_eval_cache[target]
                target_fingerprint = target_handler.compute_fingerprint(target_cooked)
                # In theory output fingerprint should not be empty given we've just run
                # the rule generating it, but the rule may be broken or a concurrent
                # removal has just occured...
                if target_fingerprint is not None:
                    target_fingerprints.append((target, target_fingerprint))
                targets_eval_cache[target] = (target_cooked, target_handler, rebuild_needed)
            db.set_rule_previous_run(run_fingerprint, target_fingerprints)

            return rebuild_needed

        with DB.connect(self.db_path) as db:
            return _run(rule)
