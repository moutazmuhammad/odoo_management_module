/** @odoo-module **/
// Keep server-management actions on the SAME record.
//
// The old code refreshed the view with the core `soft_reload` client action,
// which calls `action.restore()` and RE-CREATES the current controller. On a
// form that re-instantiation can drop the record id and dump the user on a blank
// "Create" form (and from a list/pager it jumps to a different row). That is the
// "pressing a button sends me to a new record" bug.
//
// Instead we reload only the CURRENT view's DATA in place, preserving the record
// (and the list selection), via a shared application-bus event. Both the form
// controller (here) and the Instances list controller (stage_autorefresh.js)
// listen for it.
import { FormController } from "@web/views/form/form_controller";
import { formView } from "@web/views/form/form_view";
import { registry } from "@web/core/registry";
import { useBus } from "@web/core/utils/hooks";

// Application-bus event name. `env.bus` is the global bus shared by every
// service and component, so the ops service, the form and the list all talk
// over it.
export const SM_RELOAD = "server_mgmt:reload";

// Drop-in replacement for the core `soft_reload` client action: refresh the
// current view in place (no controller restore, so no record jump). If no
// server-management view is mounted it is a harmless no-op.
registry.category("actions").add("server_mgmt_soft_reload", (env) => {
    env.bus.trigger(SM_RELOAD);
});

// Form controller used by the Server and Instance forms (js_class="server_mgmt_form").
// On a server-management op it reloads THIS record in place.
export class ServerMgmtFormController extends FormController {
    setup() {
        super.setup();
        useBus(this.env.bus, SM_RELOAD, () => this._smReload());
    }

    async _smReload() {
        const rec = this.model.root;
        // Never clobber unsaved edits or a half-typed new record — just skip the
        // refresh in that case (the user can reload manually).
        if (rec && rec.resId && !rec.isDirty && !rec.isNew) {
            await rec.load();
        }
    }
}

registry.category("views").add("server_mgmt_form", {
    ...formView,
    Controller: ServerMgmtFormController,
});
