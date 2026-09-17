# MMRoIP core endpoint for ESP-IDF (MMROIP-CORE-SPEC.md). The application supplies the device profile:
# the functions declared in include/mmroip_profile.h.
idf_component_register(
    SRCS "src/mmroip.c" "src/base64.c" "src/control.c" "src/discovery.c" "src/dns_server.c" "src/emit.c"
         "src/http_api.c" "src/net.c" "src/params.c" "src/portal.c" "src/profile_defaults.c" "src/store.c" "src/udp_control.c" "src/value.c"
    INCLUDE_DIRS "include"
    PRIV_INCLUDE_DIRS "src"
    PRIV_REQUIRES esp_app_format esp_eth esp_event esp_http_server esp_netif esp_timer esp_wifi lwip nvs_flash)
