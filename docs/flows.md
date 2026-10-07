# Flows: Scratch Jr / Scratch 3 style editor and n8n-style execution

> Data model: `flows`, `flow_versions` (immutable JSON definition), `flow_runs` and `flow_run_steps`
> (partitioned). See `docs/data-model.md`. This document defines the **JSON** stored in
> `flow_versions.definition`, which is also the format the AI edits and that is imported/exported.

## 1. Two editors, one format

| Mode | For whom | How it looks | Expressible |
|---|---|---|---|
| **Junior** (like Scratch Jr) | Non-technical users | Large icon blocks, chained left to right, one strip per trigger | Linear sequences + "wait for reply" + simple "if they reply X / Y" |
| **Advanced** (like Scratch 3) | Admins / power users | Interlocking blocks by category (colors), C-shaped blocks for if/repeat, variables and operators | Everything: conditions, loops, variables, AI, HTTP, data |

Both edit **the same JSON**. A Junior flow always opens in Advanced; an Advanced flow opens in Junior only if it
uses Junior-compatible blocks (otherwise it is shown read-only with "open in Advanced").

## 2. Definition (schema_version 1)

```jsonc
{
  "schema_version": 1,
  "variables": [{ "name": "presupuesto", "type": "number", "default": null }],
  "scripts": [                       // a script = a "hat" block (trigger) + its stack
    {
      "id": "s1",
      "trigger": { "type": "inbound_message", "config": { "keywords": ["precio"] } },
      "blocks": [ /* Block[] in sequence */ ],
      "position": { "x": 40, "y": 40 }   // only for the canvas
    }
  ]
}
```

```jsonc
// Block
{
  "id": "b7",                       // unique within the version (steps are logged by id)
  "type": "send_text",              // catalog type
  "inputs": { "text": "Hola {{contact.name}}" },   // literal values or expressions
  // only C-shaped blocks: if → then/else · repeat → body · switch_reply/ai_decide → one key per option + "other"
  "branches": { "then": [/*Block*/], "else": [/*Block*/] },
  "disabled": false
}
```

**Expressions**: `{{contact.name}}`, `{{vars.presupuesto}}`, `{{last_message.text}}`, `{{conversation.group}}`,
`{{fields.ciudad}}`, `{{ai.result.field}}`. Operator blocks produce structured values
(`{"op": ">", "left": "{{vars.presupuesto}}", "right": 50000000}`) instead of free-form code: no
arbitrary code is ever executed.

## 3. Block catalog

Colors follow Scratch 3's convention by category.

| Category | Block (`type`) | Junior | Inputs | Notes |
|---|---|---|---|---|
| **Events** (hat) | `inbound_message`, `keyword`, `handoff`, `close`, `schedule`, `webhook`, `manual`, `campaign_reply` | ✔ | trigger config | Only one per script; equals `flows.trigger_type` |
| **Messages** | `send_text` | ✔ | text | Respects the 24 h window |
| | `send_media` | ✔ | resource_id / url, caption | From the resource library |
| | `send_buttons` | ✔ | text, buttons[≤3] | Interactive buttons |
| | `send_list` | – | text, sections | Interactive list |
| | `send_template` | ✔ | name, language, values | Outside the 24 h window |
| | `send_product` | ✔ | sku / search | Catalog |
| **Wait** | `wait_reply` | ✔ | timeout_min, save_to | The run goes to `waiting` |
| | `wait_time` | ✔ | minutes | `resume_at` |
| **Control** | `if` | ✔ (simple) | condition | branches then/else |
| | `switch_reply` | ✔ | options[], timeout_min? | branch per option label + `other`; with timeout_min it waits for the reply itself |
| | `repeat` | – | times | `body` branch, max 20 iterations |
| | `stop` | ✔ | – | |
| | `go_to_script` | – | script_id | |
| **AI (Cortex)** | `ai_reply` | ✔ | ai_agent_id, instruction | The agent answers with its knowledge, memory and catalog |
| | `ai_extract` | – | schema (fields), save_to | Structured JSON through the Cortex |
| | `ai_classify` | – | options[], save_to | E.g. intent |
| | `ai_decide` | – | question, options[] | branch per option |
| **Conversation** | `handoff_to_agent` | ✔ | group_id, reason | Transfers to a human (`handoff` is the event) |
| | `assign` | – | agent_id / group_id | |
| | `tag` / `untag` | ✔ | tag | conversation_tags (source flow) |
| | `typify_close` | ✔ | typification | Closes with a tipificación |
| **CRM** | `set_field` | ✔ | field, value | contact_field_values (source flow, audited) |
| | `set_stage` | ✔ | stage | |
| | `update_memory` | – | text / from AI | Client memory |
| | `create_followup` | ✔ | agent, in_hours, note | |
| | `book_appointment` | – | date, time | |
| **Data** | `set_var` / `change_var` | – | name, value / delta | Flow variables |
| | `http_request` | – | method, url, headers, body, save_to | Allowlisted HTTPS only; secrets from Vault |
| **Operators** (reporters) | `eq`, `gt`, `lt`, `and`, `or`, `not`, `contains`, `join`, `length` | – | left/right | Only inside `condition` inputs, as `{"op","left","right"}` |

## 4. Execution (n8n style)

- One `flow_run` per trigger firing; variables and the execution pointer in `context`.
- Each block executed → one `flow_run_steps` row (input, output, status, latency) = auditable history.
- `wait_reply` / `wait_time` → status `waiting` + `resume_at`; the worker resumes them (partial index
  `flow_runs_waiting_idx`). The customer's reply resumes the waiting run before the bot answers.
- Priority: active flows by `priority`; if a flow takes the conversation, the AI bot doesn't answer that message.
- Limits: 200 steps per run, 20 iterations per `repeat`, `http_request` 10 s.
- `actor_type = 'flow'` on every change (events attribute the action to the flow).

## 5. Editing with AI

`POST /api/ai/json-edit` with `entity_type = flow`: the LLM receives the definition + this catalog as a JSON
Schema and returns the complete new definition. It is validated (schema + business rules: unique ids, one hat per
script, existing references) and saved as a **new version** `flow_versions` with `created_by_ai = true` and
`ai_prompt`. The editor shows the diff before it is published.
