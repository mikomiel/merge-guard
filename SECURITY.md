# Security policy

## Reporting a vulnerability

Use GitHub's private vulnerability reporting: open this repository's **Security**
tab and choose **Report a vulnerability**. That private advisory is the only
supported channel; do not open a public issue for a suspected vulnerability.

Include the affected commit or release, the steps or payload that demonstrates
the problem, and the impact you believe it has. We will answer in the advisory
thread.

## Scope

merge-guard binds a merge to a review decision and a CI verdict for the same
exact head. In scope: a refusal that should happen and does not, evidence from
one head satisfying another, and a merge path that bypasses the gate. Findings
about a project that merely *uses* merge-guard belong to that project.
