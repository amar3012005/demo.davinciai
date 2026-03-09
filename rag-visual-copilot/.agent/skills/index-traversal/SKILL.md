# SKILL: PageIndex Tree Traversal
**Goal**: Navigate complex SPAs using the logical site_map.json instead of vector similarity.

## Instructions:
1. **Locate**: Match the current `URL` to a `node_id` in the JSON tree using `path_regex`.
2. **Pathfind**: Identify the `Target Node` that matches the user's goal.
3. **Transition**: Generate a strategic sequence of clicks to move from the Current Node to the Target Node.
4. **Restriction**: NEVER suggest clicking a sidebar link for a section that is already marked as 'active' in the tree.

## Constraints:
- Do not use Vision Bootstrap if the text-based evidence in the DOM already contains the answer.
- Always bundle at least 2 actions (Click + Wait) for navigational transitions.
