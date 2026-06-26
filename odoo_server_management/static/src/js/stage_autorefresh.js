/** @odoo-module **/
// Live status for the Instances (server.stage) list: every 30s, re-probe the
// visible instances and reload, so running/stopped updates without anyone
// pressing "Check Status". Used via js_class="server_stage_list" on the tree.
import { ListController } from "@web/views/list/list_controller";
import { listView } from "@web/views/list/list_view";
import { registry } from "@web/core/registry";
import { useService } from "@web/core/utils/hooks";
import { onWillUnmount } from "@odoo/owl";

const REFRESH_MS = 30000;

export class ServerStageListController extends ListController {
    setup() {
        super.setup();
        this.orm = useService("orm");
        this._ssBusy = false;
        this._ssTimer = setInterval(() => this._ssAutoRefresh(), REFRESH_MS);
        onWillUnmount(() => clearInterval(this._ssTimer));
    }

    async _ssAutoRefresh() {
        // Skip if a refresh is already running or the user is editing a row.
        if (this._ssBusy || (this.model.root && this.model.root.editedRecord)) {
            return;
        }
        this._ssBusy = true;
        try {
            const ids = (this.model.root.records || [])
                .map((r) => r.resId)
                .filter(Boolean);
            if (ids.length) {
                // Fresh liveness probe of exactly the visible instances.
                await this.orm.call("server.stage", "action_check_status", [ids]);
            }
            await this.model.root.load();
        } catch (e) {
            // Transient (network/probe) errors must not break the open view.
        } finally {
            this._ssBusy = false;
        }
    }
}

registry.category("views").add("server_stage_list", {
    ...listView,
    Controller: ServerStageListController,
});
