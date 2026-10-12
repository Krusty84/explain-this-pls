## 1. Study scope

The entry point starts the worker.

*Sources: <code>vendor/worker/worker.py:12–18</code> [E-002](#source-e-002), <code>app.py:1–1</code> [E-001](#source-e-001), <code>config.py:3–5</code> [E-003](#source-e-003) (QUOTE_MISMATCH; quote not verified)*

Repeated explanation.

*Sources: <code>vendor/worker/worker.py:12–18</code> [E-002](#source-e-002), <code>app.py:1–1</code> [E-001](#source-e-001)*

## 2. System context

Introduction with no registered claim.  

## 3. System components

Repeated explanation.

*Sources: <code>app.py:1–1</code> [E-001](#source-e-001), <code>config.py:3–5</code> [E-003](#source-e-003) (QUOTE_MISMATCH; quote not verified)*

| Component | Role |
| --- | --- |
| Worker | Processes jobs |

*Sources: <code>vendor/worker/worker.py:12–18</code> [E-002](#source-e-002), <code>app.py:1–1</code> [E-001](#source-e-001)*

## 4. Startup and execution

```plantuml
@startuml
## This is diagram text
Actor -> Worker: start
@enduml
```

*Sources: <code>vendor/worker/worker.py:12–18</code> [E-002](#source-e-002), <code>app.py:1–1</code> [E-001](#source-e-001)*

## 5. Data and state

Not applicable in this synthetic fixture.

## 6. Cross-cutting mechanisms

Not applicable in this synthetic fixture.

## 7. Constraints and legacy details

Not applicable in this synthetic fixture.

## 8. Change navigation

Not applicable in this synthetic fixture.

## 9. Unknown and unverified areas

Runtime deployment is unknown.

*Limitation: no source evidence for <code>C-003</code> (UNKNOWN).*

## 10. Evidence basis and sources

Not applicable in this synthetic fixture.

### Source registry

| Evidence | Source | File | Start line | End line | Locator status | Source identity |
| --- | --- | --- | --- | --- | --- | --- |
| <a id="source-e-001"></a><code>E-001</code> | <code>source-001</code> | <code>app.py</code> | 1 | 1 | RESOLVED; locator resolved; no quote supplied | <code>{&quot;branch&quot;:&quot;main&quot;,&quot;commit&quot;:&quot;aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa&quot;,&quot;mode&quot;:&quot;git&quot;}</code> |
| <a id="source-e-002"></a><code>E-002</code> | <code>source-002</code> | <code>vendor/worker/worker.py</code> | 12 | 18 | RESOLVED; locator resolved; no quote supplied | <code>{&quot;commit&quot;:&quot;bbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb&quot;,&quot;mode&quot;:&quot;git&quot;,&quot;parent&#95;commit&quot;:&quot;aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa&quot;,&quot;submodule&#95;path&quot;:&quot;vendor/worker&quot;}</code> |
| <a id="source-e-003"></a><code>E-003</code> | <code>source-001</code> | <code>config.py</code> | 3 | 5 | QUOTE_MISMATCH; quote not verified | <code>{&quot;branch&quot;:&quot;main&quot;,&quot;commit&quot;:&quot;aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa&quot;,&quot;mode&quot;:&quot;git&quot;}</code> |
