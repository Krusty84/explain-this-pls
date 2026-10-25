# Program checks and agent assessments

Policy checks not satisfied. Review may be incomplete or have limitations/issues.

Semantic review quality is not measured. Resolving a locator does not validate the conclusion.

Agent completion assessment: Complete

Material issues reported by the agent

Frozen registry coverage (not all facts in the document): 1/1

Agent reported a contradiction: 1


## Full claim registry

| ID | Document location | Statement and scope | Author classification | Agent assessment | Sources | Limitations |
| --- | --- | --- | --- | --- | --- | --- |
| <a id="report-claim-c-001"></a><code>C-001</code> | [<code>study.original.md:3–3, study.original.md:13–19, study.original.md:29–35</code>](study.original.md)<br>[1. Study scope — block 1](ARCHITECTURE.md#report-block-b-001-001), [3. System components — block 2](ARCHITECTURE.md#report-block-b-003-002), [5. Data and state — block 2](ARCHITECTURE.md#report-block-b-005-002) | Existing statement 1. / Static source inspection. | Fact | Agent reported a contradiction | [<code>src/Заказы и счета.py:1–1</code>](#report-source-study-e-001) |   |

## Agent findings

<a id="report-finding-f-001"></a>

### <code>F-001</code> — HIGH

Factual error: Entry point description

The documented entry point is wrong.

Use the existing worker entry point.

[<code>C-001</code>](#report-claim-c-001)

[<code>src/Заказы и счета.py:1–1</code>](#report-source-study-e-001)

### Omission search — agent reports

| Area | State | Limitation |
| --- | --- | --- |
| 2. System context | Inspected according to the agent |  |
| 3. System components | Inspected according to the agent |  |
| 4. Startup and execution | Inspected according to the agent |  |
| 5. Data and state | Inspected according to the agent |  |


## Full source registry

Source state matched at checked boundaries. This is not continuous immutability. Locators were resolved by the orchestrator; this is not a record of agent file reads.

| ID | File:lines | Check | Encoding | Source identity | Used in |
| --- | --- | --- | --- | --- | --- |
| <a id="report-source-study-e-001"></a><code>study:E-001</code> | <code>src/Заказы и счета.py:1–1</code> | Resolved; locator resolved; no quote supplied | <code>utf-8</code> | <code>{&quot;directory&quot;: &quot;/example/Исходный код&quot;, &quot;fingerprint&quot;: &quot;ffffffffffffffffffffffffffffffffffffffffffffffffffffffffffffffff&quot;, &quot;mode&quot;: &quot;folder&quot;}</code> | [Existing statement 1.](#report-claim-c-001), [The documented entry point is wrong.](#report-finding-f-001) |
