---
name: review-pr
description: Use when asked to review a GitHub pull request, PR link is shared, or user says /review-pr
---

# Review GitHub Pull Request

Review the GitHub pull request: $ARGUMENTS.

## Steps

IMPORTANT:
- If the local commit does not match the pr one, checkout the PR locally using 'gh pr checkout'.
- CRITICAL: If 'gh pr checkout' fails for ANY reason, you MUST immediately STOP.
    - Do NOT attempt any workarounds (git fetch, alternative methods, etc.).
    - Do NOT proceed with the review using only diffs.
    - ALERT about the failure and WAIT for instructions.
    - This is a hard requirement - no exceptions.
- When checked out locally, ensure the local commit hash matches the remote one.
    - CRITICAL: if the commits don't match, you MUST immediately STOP.
- DO NOT make any changes to the code
- Be constructive and specific in your comments
- Suggest improvements where appropriate
- Only provide review feedback in the CONSOLE. DO NOT ACT ON GITHUB.
- No need to run tests or linters, just review the code changes.
- Always check if existing helper functions exist. These should be used in favour of new code in providers. Example: if a music provider does pls parsing, the author should rewrite this to use `parse_pls` from `music_assistant.helpers.playlists` instead of writing their own parsing code.

## Output Format

List specific comments per file/line that need attention. Do not list things that are already correct.

Each comment:
1. State the severity ([CRITICAL], [PROBLEM], [SUGGESTION])
2. State the problem (1 sentence)
3. Why it matters (1 sentence, if needed)
4. Suggested fix (snippet or specific action)

Example:
> This could generate a `KeyError` if `"name"` does not exist in the `dict`. Consider using `.get("name")` or adding a check.

End with an overall assessment and summary:

```
Overall assessment: <approve | request changes | comment>
- [CRITICAL] <issue>
- [PROBLEM] <issue>
- [SUGGESTION] <improvement>
```

## Constraints

- DO NOT make any changes to the code.
- No need to run tests or linters, just review the code changes.
