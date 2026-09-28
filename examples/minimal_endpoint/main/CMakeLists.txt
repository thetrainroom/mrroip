# main.c is the endpoint; each h_*.c includes one public header and nothing else, so the build
# fails if a header stops standing on its own (that is how mrroip.h's missing <stdint.h> was found).
idf_component_register(SRCS "main.c"
                            "h_mrroip.c" "h_mrroip_base64.c" "h_mrroip_emit.c" "h_mrroip_net.c"
                            "h_mrroip_params.c" "h_mrroip_profile.c" "h_mrroip_value.c")
