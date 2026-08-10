# ForgeOps — FBC Dev Stack Project

See **[mock-tenant.md](mock-tenant.md)** for the full user, developer, and design guide — architecture, prerequisites, deploy guide, implementation details (all changed files), AM tree config, SaaS sync plan, tenant shim, operational runbook, and known issues.

---

## Quick Start

```sh
# Once per OrbStack instance — installs cluster prerequisites:
python3 bin/mock-tenant.py bootstrap
# Also add to /etc/hosts: 127.0.0.1 mock.iam.example.com

# Deploy the application stack (AM, IDM, DS, Gitea, tenant shim):
python3 bin/mock-tenant.py deploy
```

**Always use `python3 bin/mock-tenant.py` for all operations.** Never use manual kubectl/forgeops steps.

---

## Key Facts

- **Branch:** `wajih-mock-tenant`
- **Namespace:** `fr-platform`
- **Kubernetes runtime:** OrbStack (`kubectl config use-context orbstack`)
- **Platform FQDN:** `mock.iam.example.com`
- **Gitea:** `http://gitea.fr-platform.svc.cluster.local:3000` (port-forward: `kubectl port-forward -n fr-platform svc/gitea 3000:3000`)
- **AM:** `https://mock.iam.example.com/am`
- **IDM:** `https://mock.iam.example.com/openidm`
- **Admin UI:** `https://mock.iam.example.com/platform`
- **Browser tunnel:** `bin/tunnel` (requires `sudo`)

---

## Related Docs

- [mock-tenant.md](mock-tenant.md) — comprehensive guide (this is the main doc)
- [colima.md](colima.md) — Colima notes (superseded by OrbStack, kept for historical reference)
- `/Users/wajih.ahmed/source/github.com/ForgeCloud/saas/CLAUDE.md` — saas repo investigation notes
