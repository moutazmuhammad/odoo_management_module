# Security Roles Redesign — 4 groups

Hierarchy (each includes the one below): **Developer ⊂ Operational ⊂ DevOps ⊂ Admin**

## Requested
1. **Admin** — whole module (incl. General Settings).
2. **DevOps** — everything **except General Settings**.
3. **Operational** — Servers (create) + only **Discover Instances** & **Install Self‑Backup
   Agent**; Stages: see **Branch Configuration** + **Backups** + action buttons.
4. **Developer** — like Operational but **Stages only** (no Servers); **cannot backup a
   Client Server (client_stage=True) stage**.

## Proposed capability matrix
| Capability | Developer | Operational | DevOps | Admin |
|---|:--:|:--:|:--:|:--:|
| Stages menu | ✓ | ✓ | ✓ | ✓ |
| Stage buttons: Start/Stop/Restart, Backup, Pull, Upgrade, View Logs, Check Status, View Conf | ✓* | ✓ | ✓ | ✓ |
| Backup a **Client Server** stage | ✗ | ✓ | ✓ | ✓ |
| Stage page: **Branch Configuration** | ✓ | ✓ | ✓ | ✓ |
| Stage page: **Backups** | ✓ | ✓ | ✓ | ✓ |
| Stage page: **General Info** | ✗ | ✗ | ✓ | ✓ |
| Stage page: **Access Info** (master password) | ✗ | ✗ | ✓ | ✓ |
| **Servers** menu + create | ✗ | ✓ | ✓ | ✓ |
| Server: **Discover Instances** | ✗ | ✓ | ✓ | ✓ |
| Server: **Install Self‑Backup Agent** | ✗ | ✓ | ✓ | ✓ |
| Server: Test Connection / Check Status / Open Terminal / Run Backup Now | ✗ | ✗(?) | ✓ | ✓ |
| Repository Management (Git Repos, Stage Repo Paths) | ✗ | ✗ | ✓ | ✓ |
| **General Settings** | ✗ | ✗ | ✗ | ✓ |

\* Developer can't act on Client Server stages (enforced server‑side, as today).

## Implementation outline
- `security.xml`: keep `group_user`(=Developer), `group_operator`(=Operational),
  `group_admin`(=Admin); **add `group_devops`** between operator and admin.
  Implications: admin → devops → operator → user.
- Relabel the groups (Developer / Operational / DevOps / Administrator).
- Views: move the currently **admin‑only** server buttons (Test Connection, Check Status,
  Open Terminal, Run Backup Now) and the **General Info / Access Info** pages to
  **DevOps**; keep **General Settings** menu Admin‑only; keep **Discover** & **Install
  Agent** at Operational.
- Code: `action_deploy_agent` → Operational (was Admin); settings/Test Storage → Admin.
- ACLs: server.github.settings = Admin only; server.host create = Operational.
- Migration: map existing users — Admin→Admin, Operator→Operational; (no DevOps assigned
  automatically — you grant it).

## STATUS: DONE — deployed v1.18 (confirmed answers applied)
- Groups: Developer ⊂ Operational ⊂ DevOps ⊂ Admin (verified live).
- DevOps = everything-but-settings (sees secrets/conf/terminal/all server buttons).
- Operational server buttons = **Discover + Install Agent only** (Test Connection,
  Check Status, Terminal, Run Backup Now → DevOps+). `action_deploy_agent` → Operational.
- Developer/Operational stage form shows **only Branch Config + Backups + buttons**;
  General Info & Access Info → DevOps+. Detected Instances tab → DevOps+.
- Repository Management menu → DevOps+; General Settings menu → Admin only.
- Signup → Developer (base) only (already the case).
- **DevOps Only** flag on a server (`devops_only`) + ir.rules: such a server AND its
  instances are visible only to DevOps/Admins (Operational can't see them).
- Extra defaults: SSH port **7812**; "Stop Instances" **false**.
- No data migration needed (group XML ids unchanged; existing User→Developer,
  Operator→Operational, Admin→Admin).

## Original open questions (answered)
A. **DevOps sees secrets?** Master passwords (Access Info), conf files, web terminal —
   show to DevOps? (Default: yes — "everything except settings".)
B. **Operational server buttons** — strictly only Discover + Install Agent, or also
   **Test Connection** (normally needed to onboard a new server)?
C. **Hide General Info & Access Info** pages from Developer/Operational (so they truly see
   only Branch Config + Backups)? (Default: yes.)
D. **Repository Management** (Git Repos, Stage Repo Paths) — DevOps too, or Admin only?
