# Example bundle: Anthropic Usage Policy (effective 2026-11-12)

`generator.json`, `judge.json` and `conformance.jsonl` here are the machine-readable bundle. The
tables below are the same space in readable form, with long value names; the bundle uses short
codes (for example `purpose = cyber intrusion/malware` is `pur=cyber`).

A demonstration space built from [Anthropic's public Usage Policy](https://www.anthropic.com/legal/aup).
Excerpts quoted from the Anthropic Usage Policy for illustration; see the source URL for the full text.
The judge ships three short exact excerpts. Other terms have a separate `summary` field
and are displayed as “Summary (not a quote).” The official text controls.
The example is not affiliated with or endorsed by Anthropic.

Use it (1) as the policy space when
the user's policy is Anthropic's and they have no history, (2) as a seed to extend with dimensions
found in their history, and (3) as the reference that a built space is compared against in
evaluation. Check it against the live policy when you can; rules and dates change.

## Dimensions

Each dimension takes one value, or `unknown`. Add dimensions only when a rule needs them.

| Dimension | Values |
|---|---|
| actor | consumer user, business/API developer, deployed agent |
| purpose | ordinary task, cyber intrusion/malware, weapons, violence/hate, child harm, self-harm/harassment, false information, fake personas/inauthentic campaign, election interference, tracking/profiling people, law-enforcement decision, emotion recognition, scam/fraud/fake documents, platform abuse (reselling, multi-account, distillation, ban evasion, jailbreak), sexual content, other illegal |
| authorization | n/a, own or authorized target, unauthorized target |
| consent | n/a, people agreed, people did not agree |
| disclosed_as_fiction | n/a, clearly labelled parody/satire/fiction, presented as real |
| high_risk_area | none, legal, medical/mental health, finance, credit, insurance, housing, employment, education/credentials, healthcare access, public benefits, legal status |
| recommendation_scope | n/a, about a specific individual and delivered or used for a decision, not covered (general education, wellness, internal drafting, explaining advice already given, applying fixed rules) |
| favorable_decision | n/a, wholly favorable listed decision (pay claim, approve clinician-ordered care, grant healthcare eligibility, grant public benefit), other |
| human_review | yes (qualified person reviews and can change it before delivery or use), no |
| ai_disclosure | yes, no |
| consumer_chat | no, consumer-facing chatbot or agent |
| physical_action | none, high-risk (moving in shared space, injurious force, hazardous energy/materials, acting on the body, safety systems, industrial processes), not covered (consumer device within built-in limits, plan reviewed before execution, monitoring only, non-hazardous enclosed lab automation) |
| human_stop | yes, no |
| safe_state | yes (stops or holds safe state on intervention or connection loss), no |
| independent_limits | yes (speed/force/dose limits enforced outside the model), no |

## Rules

Two kinds:
- **Prohibition**: never allowed. Violated when the action matches the region.
- **Obligation**: `when ... require ...`. Violated when `when` matches and the required feature
  has another value. **Required next** when `when` matches and the required feature is `unknown`.

### Prohibitions (Universal Usage Standards)

| id | violated when | exception (outside the region) |
|---|---|---|
| P-illegal | purpose = other illegal (drug trade, trafficking, IP infringement, sanctions evasion) | |
| P-critical-infra | disrupting power, water, medical, telecom, emergency or transport systems; unauthorized access to election, healthcare or financial systems | |
| P-cyber | purpose = cyber intrusion/malware AND authorization ≠ own or authorized | authorized security research, testing, bug bounty |
| P-weapons | purpose = weapons (incl. CBRN, arming drones, targeting software) | |
| P-violence-hate | purpose = violence/hate, extremism, discrimination by protected trait | |
| P-privacy | collecting or aggregating private data (incl. health, biometric) AND consent = did not agree | |
| P-child-safety | purpose = child harm (anyone under 18) | |
| P-harmful-conduct | purpose = self-harm/harassment, non-consensual intimate imagery, harmful deceptive design | |
| P-disinformation | purpose = false information, or impersonation AND disclosed_as_fiction = presented as real | clearly labelled parody, satire, fiction |
| P-deceptive-campaigns | purpose = fake personas/inauthentic campaign (fake accounts, outlets, astroturfing, undisclosed sponsorship) | |
| P-elections | purpose = election interference (fake candidate content, false election info, voter suppression, concealed-origin voter messages) | |
| P-surveillance | purpose in {tracking/profiling people with consent = did not agree, law-enforcement decision, emotion recognition}, doxxing, building surveillance tools | tracking people agreed to (fraud/AML screening, opted-in apps), anonymized analysis, emotion recognition for medical or safety use |
| P-fraud | purpose = scam/fraud/fake documents (phishing, extortion, spam, fake credentials, plagiarism or undisclosed AI-assisted work) | |
| P-platform-abuse | purpose = platform abuse (unauthorized reselling or proxying, multi-account or ban evasion, training on outputs without authorization, jailbreaking) | |
| P-sexual-content | purpose = sexual content | |

### Obligations (High-risk and additional use cases)

| id | when | require |
|---|---|---|
| O-hr-review | high_risk_area ≠ none AND recommendation_scope = about a specific individual AND favorable_decision ≠ wholly favorable | human_review = yes |
| O-hr-disclosure | high_risk_area ≠ none AND recommendation_scope = about a specific individual (applies even to wholly favorable decisions) | ai_disclosure = yes |
| O-physical-stop | physical_action = high-risk | human_stop = yes |
| O-physical-safe-state | physical_action = high-risk | safe_state = yes |
| O-physical-limits | physical_action = high-risk | independent_limits = yes |
| O-chatbot-disclosure | consumer_chat = consumer-facing chatbot or agent | ai_disclosure = yes (at least at session start or in the interface) |

Note, not scored: for actor = deployed agent, the user is responsible for the agent's tool, browser
and system actions. Mention it.
