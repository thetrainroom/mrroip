/* The smallest possible MRRoIP endpoint: the core plus the profile hooks it requires, nothing else.
   The three profile_object_stream_* hooks are left out on purpose, to prove the weak defaults cover them. */
#include "mrroip.h"
#include "mrroip_profile.h"

static const char *const modes[] = { "idle", NULL };
static const profile_info_t info = { .device_type = "null", .device_class = "passive", .profile_version = "0.1",
                                     .modes = modes, .rest_mode = "idle" };

const profile_info_t *profile_info(void) { return &info; }
const param_desc_t *profile_params(size_t *count) { *count = 0; return NULL; }
void profile_start(void) { }
void profile_param_changed(const char *name) { (void)name; }
void profile_emit_objects(emit_t *objects) { (void)objects; }
void profile_emit_state(emit_t *profile, bool *busy, const char **fault) { (void)profile; *busy = false; *fault = NULL; }
const char *profile_fault(void) { return NULL; }
const char *profile_etag(void) { return "null-0.1"; }
const char *profile_object_check(const char *id, const object_value_t *value) { (void)id; (void)value; return "unknown_object"; }
void profile_apply(const char *mode, size_t count, const char *const ids[], const object_value_t *const values[])
{ (void)mode; (void)count; (void)ids; (void)values; }
void profile_estop(void) { }
void profile_reset(void) { }
void profile_come_to_rest(void) { }
void profile_message(profile_msg_t kind, bool accepted) { (void)kind; (void)accepted; }

void app_main(void)
{
    mrroip_init();
    profile_start();
    mrroip_start(&(mrroip_config_t){ .ethernet = NULL });
}
