#pragma once

#include "driver/gpio.h"

#ifdef __cplusplus
extern "C" {
#endif

typedef struct {
    gpio_num_t bclk;
    gpio_num_t ws;
    gpio_num_t din;
    gpio_num_t dout;
} board_i2s_pin_map_t;

const board_i2s_pin_map_t *board_get_rx_i2s_pins(void);
const board_i2s_pin_map_t *board_get_tx_i2s_pins(void);

#ifdef __cplusplus
}
#endif
