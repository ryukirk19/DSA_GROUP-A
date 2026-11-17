"""TODO: select one public action from each immutable world observation."""
import heapq
from src.api import WorldAction, WorldSnapshot

_STEPS = (
    (WorldActionType.MOVE_NORTH, -1, 0),
    (WorldActionType.MOVE_SOUTH, 1, 0),
    (WorldActionType.MOVE_WEST, 0, -1),
    (WorldActionType.MOVE_EAST, 0, 1),
)


class AgentController:
    """A new controller is constructed for each run; arrival still needs EXIT."""

    def __init__(self) -> None:
        pass

    def choose_action(self, state: WorldSnapshot, agent_id: str) -> WorldAction:
        # Return an exact WorldAction; illegal responses are rejected, not changed to WAIT.
        if state.level.patrols:
            plan = self._patrol_plan(state)
        else:
            plan = self._static_plan(state)
        if plan:
            return plan[0]
        return self._fallback(state)

    def _static_plan(self, state):
        level = state.level
        artifacts = level.artifacts
        enemies = level.enemies
        allowed = frozenset(state.allowed_actions)
        artifact_at = {}
        for index, artifact in enumerate(artifacts):
            artifact_at.setdefault((artifact.position.row, artifact.position.column), []).append(index)
        artifact_index = {artifact.artifact_id: index for index, artifact in enumerate(artifacts)}
        enemy_index = {enemy.enemy_id: index for index, enemy in enumerate(enemies)}
        collected_mask = 0
        for artifact_id in state.collected_artifact_ids:
            index = artifact_index.get(artifact_id)
            if index is not None:
                collected_mask |= 1 << index
        defeated_mask = 0
        for enemy_id in state.defeated_enemy_ids:
            index = enemy_index.get(enemy_id)
            if index is not None:
                defeated_mask |= 1 << index
        past_penalty = sum(enemy.encounter_penalty for index, enemy in enumerate(enemies)
                           if defeated_mask & (1 << index))
        static = list(enumerate(enemies))
        start = ((state.agent_position.row, state.agent_position.column),
                 collected_mask, defeated_mask)
        distances = {start: 0}
        parents = {start: None}
        queue = [(0, 0, start)]
        sequence = 0
        remaining = state.max_turns - state.turn
        exit_cell = (level.exit.row, level.exit.column)
        goals = []

        def push(current, next_key, next_cost, action):
            nonlocal sequence
            if next_cost > remaining:
                return
            old = distances.get(next_key)
            if old is not None and old <= next_cost:
                return
            distances[next_key] = next_cost
            parents[next_key] = (current, action)
            sequence += 1
            heapq.heappush(queue, (next_cost, sequence, next_key))

        while queue:
            cost, _, current = heapq.heappop(queue)
            if distances.get(current) != cost:
                continue
            if cost > remaining:
                continue
            cell, mask, defeated = current
            if cell == exit_cell and WorldActionType.EXIT in allowed:
                total_cost = state.turn + cost + 1 + past_penalty
                reward = sum(artifact.value for index, artifact in enumerate(artifacts)
                             if mask & (1 << index))
                goals.append((self._score(state, reward, total_cost), -total_cost, reward, current))
            for kind, dr, dc in _STEPS:
                if kind not in allowed:
                    continue
                other = (cell[0] + dr, cell[1] + dc)
                if not level.terrain.is_traversable(Position(other[0], other[1])):
                    continue
                if self._blocked(other, static, defeated):
                    continue
                push(current, (other, mask, defeated), cost + 1, WorldAction(kind))
            if WorldActionType.COLLECT in allowed:
                for index in artifact_at.get(cell, ()):
                    if not mask & (1 << index):
                        push(current, (cell, mask | (1 << index), defeated), cost + 1,
                             WorldAction(WorldActionType.COLLECT,
                                         target_id=artifacts[index].artifact_id))
            if WorldActionType.ENGAGE in allowed:
                for index, enemy in enumerate(enemies):
                    if defeated & (1 << index):
                        continue
                    if abs(enemy.position.row - cell[0]) + abs(enemy.position.column - cell[1]) == 1:
                        push(current, (cell, mask, defeated | (1 << index)),
                             cost + 1 + enemy.encounter_penalty,
                             WorldAction(WorldActionType.ENGAGE, target_id=enemy.enemy_id))
        if not goals:
            return []
        chosen = max(goals, key=lambda goal: (goal[0], goal[1], goal[2]))
        actions = []
        key = chosen[3]
        while parents[key] is not None:
            previous, action = parents[key]
            actions.append(action)
            key = previous
        actions.reverse()
        actions.append(WorldAction(WorldActionType.EXIT))
        return actions

    def _patrol_plan(self, state):
        level = state.level
        artifacts = level.artifacts
        enemies = level.enemies
        allowed = frozenset(state.allowed_actions)
        patrols = {patrol.enemy_id: patrol for patrol in level.patrols}
        static = [(index, enemy) for index, enemy in enumerate(enemies)
                  if enemy.enemy_id not in patrols]
        artifact_at = {}
        for index, artifact in enumerate(artifacts):
            artifact_at.setdefault((artifact.position.row, artifact.position.column), []).append(index)
        artifact_index = {artifact.artifact_id: index for index, artifact in enumerate(artifacts)}
        enemy_index = {enemy.enemy_id: index for index, enemy in enumerate(enemies)}
        collected_mask = 0
        for artifact_id in state.collected_artifact_ids:
            index = artifact_index.get(artifact_id)
            if index is not None:
                collected_mask |= 1 << index
        defeated_mask = 0
        for enemy_id in state.defeated_enemy_ids:
            index = enemy_index.get(enemy_id)
            if index is not None:
                defeated_mask |= 1 << index
        start_cell = (state.agent_position.row, state.agent_position.column)
        start_key = (start_cell, collected_mask, defeated_mask)
        exit_cell = (level.exit.row, level.exit.column)
        frontier = {start_key}
        parents = {}
        goals = []
        time = state.turn
        deadline = state.deadline_turn
        while time < state.max_turns and frontier:
            following = set()
            patrol_now = self._patrol_cells(patrols, time)
            patrol_next = self._patrol_cells(patrols, time + 1)
            for key in sorted(frontier):
                cell, mask, defeated = key
                if cell == exit_cell and WorldActionType.EXIT in allowed:
                    total_cost = time + 1 + sum(
                        enemy.encounter_penalty for index, enemy in enumerate(enemies)
                        if defeated & (1 << index))
                    reward = sum(artifact.value for index, artifact in enumerate(artifacts)
                                 if mask & (1 << index))
                    goals.append((self._score(state, reward, total_cost), -total_cost, reward,
                                  (time, key)))
                if time + 1 >= state.max_turns:
                    continue
                if deadline is not None and time + 1 >= deadline:
                    continue

                def visit(next_key, action):
                    marker = (time + 1, next_key)
                    if marker not in parents:
                        parents[marker] = (time, key, action)
                        following.add(next_key)

                for kind, dr, dc in _STEPS:
                    if kind not in allowed:
                        continue
                    other = (cell[0] + dr, cell[1] + dc)
                    if not level.terrain.is_traversable(Position(other[0], other[1])):
                        continue
                    if other in patrol_now or other in patrol_next:
                        continue
                    if self._blocked(other, static, defeated):
                        continue
                    visit((other, mask, defeated), WorldAction(kind))
                if cell not in patrol_next:
                    if WorldActionType.COLLECT in allowed:
                        for index in artifact_at.get(cell, ()):
                            if not mask & (1 << index):
                                visit((cell, mask | (1 << index), defeated),
                                      WorldAction(WorldActionType.COLLECT,
                                                  target_id=artifacts[index].artifact_id))
                    if WorldActionType.ENGAGE in allowed:
                        for index, enemy in static:
                            if defeated & (1 << index):
                                continue
                            if abs(enemy.position.row - cell[0]) + abs(enemy.position.column - cell[1]) == 1:
                                visit((cell, mask, defeated | (1 << index)),
                                      WorldAction(WorldActionType.ENGAGE, target_id=enemy.enemy_id))
                    if WorldActionType.WAIT in allowed:
                        visit((cell, mask, defeated), WorldAction.wait())
            frontier = following
            time += 1
        if not goals:
            return []
        chosen = max(goals, key=lambda goal: (goal[0], goal[1], goal[2]))
        actions = []
        marker = chosen[3]
        while marker in parents:
            previous_time, previous_key, action = parents[marker]
            actions.append(action)
            marker = (previous_time, previous_key)
        actions.reverse()
        actions.append(WorldAction(WorldActionType.EXIT))
        return actions

    def _patrol_cells(self, patrols, time):
        result = set()
        for patrol in patrols.values():
            phase = (patrol.initial_phase + time) % patrol.period
            position = patrol.positions[phase]
            result.add((position.row, position.column))
        return result

    def _blocked(self, cell, enemies, defeated_mask):
        for index, enemy in enemies:
            if defeated_mask & (1 << index):
                continue
            if (enemy.position.row, enemy.position.column) == cell:
                return True
        return False

    def _score(self, state, reward, total_cost):
        excess = total_cost - state.baseline_cost
        if excess < 0:
            excess = 0
        if excess >= state.zero_score_excess:
            return 0
        return max(0, state.base_points + reward - state.deduction_rate * excess)

    def _fallback(self, state):
        allowed = frozenset(state.allowed_actions)
        if WorldActionType.WAIT in allowed:
            return WorldAction.wait()
        for kind, dr, dc in _STEPS:
            if kind not in allowed:
                continue
            position = Position(state.agent_position.row + dr, state.agent_position.column + dc)
            if state.is_traversable(position):
                return WorldAction(kind)
        return WorldAction(WorldActionType.EXIT)

