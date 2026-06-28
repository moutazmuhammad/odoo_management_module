/** @odoo-module **/
// Background-action results for server management. Each long action (start/stop/
// restart/pull/upgrade/backup) runs server-side in a background thread and pushes
// its outcome over the bus to channel "server_mgmt_ops_<uid>". This service shows
// that outcome as a toast and, for a finished backup, triggers the auto-download.
import { registry } from "@web/core/registry";
import { session } from "@web/session";

const serverMgmtOpsService = {
    dependencies: ["bus_service", "notification", "action"],
    start(env, { bus_service, notification, action }) {
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
                // Status-changing actions (start/stop/restart) ask for a refresh so
                // the running/stopped badge updates in place — a soft reload of the
                // current view only, never a full browser page load.
                if (payload.reload) {
                    action.doAction({ type: "ir.actions.client", tag: "soft_reload" });
                }
            }
        });
    },
};

registry.category("services").add("server_mgmt_ops", serverMgmtOpsService);
