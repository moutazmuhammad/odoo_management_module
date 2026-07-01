/** @odoo-module **/
// Background-action results for server management. Each long action (start/stop/
// restart/pull/upgrade/backup) runs server-side in a background thread and pushes
// its outcome over the bus to channel "server_mgmt_ops_<uid>". This service shows
// that outcome as a toast and, for a finished backup, triggers the auto-download.
import { registry } from "@web/core/registry";
import { session } from "@web/session";
import { SM_RELOAD } from "@odoo_server_management/js/stage_reload";

const serverMgmtOpsService = {
    dependencies: ["bus_service", "notification"],
    start(env, { bus_service, notification }) {
        const channel = "server_mgmt_ops_" + session.uid;
        bus_service.addChannel(channel);
        bus_service.addEventListener("notification", ({ detail: notifications }) => {
            for (const { type, payload } of notifications) {
                if (type !== "server_mgmt_op") {
                    continue;
                }
                notification.add(payload.message || "", {
                    type: payload.ok ? "success" : "danger",
                    title: payload.title || "",
                    // Errors stay until dismissed; successes fade on their own.
                    sticky: !!payload.sticky,
                });
                // Backup finished → download the file automatically. An <a download>
                // navigation to a presigned URL (Content-Disposition: attachment) is
                // allowed without a user gesture (unlike window.open, which is blocked).
                if (payload.url) {
                    const a = document.createElement("a");
                    a.href = payload.url;
                    a.download = "";
                    a.style.display = "none";
                    document.body.appendChild(a);
                    a.click();
                    a.remove();
                }
                // Every finished action refreshes the current view IN PLACE (data
                // reload only — never a controller restore or full page load), so the
                // running/stopped badge updates AND any button hidden while
                // op_state == 'running' reappears, all WITHOUT leaving the current
                // record. The form/list controllers listen for this on env.bus.
                env.bus.trigger(SM_RELOAD);
            }
        });
    },
};

registry.category("services").add("server_mgmt_ops", serverMgmtOpsService);
