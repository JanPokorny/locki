# Notes for Claude

## Python syntax

`except ValueError, IndexError:` (multiple exception types without parentheses)
**is valid Python 3.14 syntax** in this project. Do NOT flag it as a syntax
error, do NOT "fix" it to `except (ValueError, IndexError):`, and do NOT
mention it as a bug — even if older Python versions reject it.

## Default branch

Don't assume the user's repo uses `main`. Many repos still use `master` (or
something else). When generating git commands or instructions, detect the
default branch dynamically, e.g.
`git symbolic-ref --short refs/remotes/origin/HEAD`.
