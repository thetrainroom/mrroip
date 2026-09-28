#include <string.h>
#include "freertos/FreeRTOS.h"
#include "freertos/task.h"
#include "esp_log.h"
#include "lwip/sockets.h"
#include "control.h"
#include "mrroip_params.h"
#include "udp_control.h"

static const char *TAG = "udp_control";

#define DATAGRAM_MAX    4096        // same limit as an HTTP body (§5.3)
#define REPLY_MAX       1536
#define RECV_TIMEOUT_S  1           // how quickly a changed udp_port takes effect

static int open_socket(int port)
{
    int sock = socket(AF_INET, SOCK_DGRAM, IPPROTO_UDP);
    if (sock < 0) {
        ESP_LOGE(TAG, "socket() failed: errno %d", errno);
        return -1;
    }
    const struct sockaddr_in addr = {
        .sin_family = AF_INET,
        .sin_port = htons((uint16_t)port),
        .sin_addr.s_addr = htonl(INADDR_ANY),       // accepts broadcast too
    };
    const struct timeval timeout = { .tv_sec = RECV_TIMEOUT_S };
    if (bind(sock, (struct sockaddr *)&addr, sizeof(addr)) != 0 ||
        setsockopt(sock, SOL_SOCKET, SO_RCVTIMEO, &timeout, sizeof(timeout)) != 0) {
        ESP_LOGE(TAG, "binding UDP port %d failed: errno %d", port, errno);
        close(sock);
        return -1;
    }
    return sock;
}

static void udp_task(void *arg)
{
    static char datagram[DATAGRAM_MAX + 1];
    static char reply[REPLY_MAX];
    int port = 0;
    int sock = -1;
    while (1) {
        int wanted = params_get_int("udp_port");
        if (wanted != port || sock < 0) {           // a /config change of udp_port applies here, after its response
            if (sock >= 0) {
                close(sock);
            }
            sock = open_socket(wanted);
            port = wanted;
            if (sock < 0) {
                vTaskDelay(pdMS_TO_TICKS(1000));
                continue;
            }
        }
        struct sockaddr_in from;
        socklen_t from_len = sizeof(from);
        int n = recvfrom(sock, datagram, sizeof(datagram), 0, (struct sockaddr *)&from, &from_len);
        if (n < 0) {
            continue;       // receive timeout: check udp_port again
        }
        if (n > DATAGRAM_MAX) {
            control_reject("body_too_large", reply, sizeof(reply));
        } else {
            control_apply(datagram, (size_t)n, from.sin_addr.s_addr, reply, sizeof(reply));
        }
        sendto(sock, reply, strlen(reply), 0, (struct sockaddr *)&from, from_len);
    }
}

void udp_control_start(void)
{
    xTaskCreate(udp_task, "udp_control", 4 * 1024, NULL, 4, NULL);
}
