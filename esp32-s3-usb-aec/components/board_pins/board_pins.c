#include "board_pins.h"

/* Current validated bring-up pin map (single-mic + single-speaker). */
static const board_i2s_pin_map_t RX_PINS = {
    .bclk = GPIO_NUM_5,
    .ws = GPIO_NUM_4,
    .din = GPIO_NUM_6,
    .dout = GPIO_NUM_NC,
};

static const board_i2s_pin_map_t TX_PINS = {
    .bclk = GPIO_NUM_15,
    .ws = GPIO_NUM_16,
    .din = GPIO_NUM_NC,
    .dout = GPIO_NUM_7,
};

const board_i2s_pin_map_t *board_get_rx_i2s_pins(void) { return &RX_PINS; }

const board_i2s_pin_map_t *board_get_tx_i2s_pins(void) { return &TX_PINS; }
