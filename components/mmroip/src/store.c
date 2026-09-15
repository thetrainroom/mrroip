#include <string.h>
#include "freertos/FreeRTOS.h"
#include "freertos/queue.h"
#include "freertos/semphr.h"
#include "freertos/task.h"
#include "esp_log.h"
#include "nvs.h"
#include "proto_name.h"
#include "store.h"

static const char *TAG = "store";

#define STORE_QUEUE_LEN     16
#define STORE_STR_MAX       64

typedef enum { OP_I32, OP_U32, OP_STR, OP_ERASE_ALL, OP_FLUSH } store_op_t;

typedef struct {
    store_op_t op;
    char key[16];               // NVS keys are at most 15 characters
    int32_t i32;
    uint32_t u32;
    char str[STORE_STR_MAX];
    SemaphoreHandle_t done;     // OP_FLUSH only
} store_msg_t;

static QueueHandle_t queue;

static void store_task(void *arg)
{
    store_msg_t msg;
    while (1) {
        xQueueReceive(queue, &msg, portMAX_DELAY);
        if (msg.op == OP_FLUSH) {
            xSemaphoreGive(msg.done);
            continue;
        }
        nvs_handle_t nvs;
        esp_err_t err = nvs_open(MMROIP_TOKEN, NVS_READWRITE, &nvs);
        if (err == ESP_OK) {
            switch (msg.op) {
            case OP_I32:        err = nvs_set_i32(nvs, msg.key, msg.i32); break;
            case OP_U32:        err = nvs_set_u32(nvs, msg.key, msg.u32); break;
            case OP_STR:        err = nvs_set_str(nvs, msg.key, msg.str); break;
            case OP_ERASE_ALL:  err = nvs_erase_all(nvs); break;
            default:            break;
            }
            if (err == ESP_OK) {
                err = nvs_commit(nvs);
            }
            nvs_close(nvs);
        }
        if (err != ESP_OK) {
            ESP_LOGE(TAG, "Writing '%s' failed: %s", msg.key, esp_err_to_name(err));
        }
    }
}

static void post(store_msg_t *msg, const char *key)
{
    strlcpy(msg->key, key, sizeof(msg->key));
    if (xQueueSend(queue, msg, pdMS_TO_TICKS(1000)) != pdTRUE) {
        ESP_LOGE(TAG, "Write queue full, '%s' not stored", key);
    }
}

void store_start(void)
{
    queue = xQueueCreate(STORE_QUEUE_LEN, sizeof(store_msg_t));
    xTaskCreate(store_task, "store", 3 * 1024, NULL, 2, NULL);
}

void store_set_i32(const char *key, int32_t value)
{
    store_msg_t msg = { .op = OP_I32, .i32 = value };
    post(&msg, key);
}

void store_set_u32(const char *key, uint32_t value)
{
    store_msg_t msg = { .op = OP_U32, .u32 = value };
    post(&msg, key);
}

void store_set_str(const char *key, const char *value)
{
    store_msg_t msg = { .op = OP_STR };
    strlcpy(msg.str, value, sizeof(msg.str));
    post(&msg, key);
}

void store_erase_all(void)
{
    store_msg_t msg = { .op = OP_ERASE_ALL };
    post(&msg, "(all)");
}

bool store_flush(TickType_t timeout)
{
    store_msg_t msg = { .op = OP_FLUSH, .done = xSemaphoreCreateBinary() };
    if (!msg.done) {
        return false;
    }
    strlcpy(msg.key, "(flush)", sizeof(msg.key));
    if (xQueueSend(queue, &msg, timeout) != pdTRUE) {
        vSemaphoreDelete(msg.done);
        return false;
    }
    if (xSemaphoreTake(msg.done, timeout) != pdTRUE) {
        return false;   // the semaphore is deliberately leaked: the store task may still give it
    }
    vSemaphoreDelete(msg.done);
    return true;
}
