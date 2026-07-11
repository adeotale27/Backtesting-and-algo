###############################################################################
#
# The MIT License (MIT)
#
# Copyright (c) Zerodha Technology Pvt. Ltd.
#
# This example shows how to subscribe and get ticks from Kite Connect ticker,
# For more info read documentation - https://kite.trade/docs/connect/v1/#streaming-websocket
###############################################################################


import sys
import os
import atexit
import signal
from common_lib import *

if len(sys.argv) < 6:
    logging.info("Format -- python3 ticker_single_scraper.py <BUY_GAP> <SELL_GAP> <SYMBOL> <QUANTITY> <REQUET_TOKEN>");
    logging.info("Sample -- python3 ticker_single_scraper.py 50 50 NIFTY22NOVFUT 50 8kC8GlDGekNReKhH87SES93Z3Lq01hvb");
    quit()


buy_gap = float(sys.argv[1]);
sell_gap = float(sys.argv[2]);
symbol = sys.argv[3];
quantity = sys.argv[4]
request_token = sys.argv[5];

set_execution_variables(symbol, buy_gap, sell_gap, quantity, request_token)


def _handle_sigterm(signum: int, frame) -> None:
    """Convert SIGTERM (dashboard Stop button) into a clean exit so atexit
    handlers run and this process's status file is removed."""
    logging.info("SIGTERM received — shutting down scraper for %s", symbol)
    sys.exit(0)


signal.signal(signal.SIGTERM, _handle_sigterm)
atexit.register(delete_own_status_file)

product_type = "NRML" 
if len(sys.argv) >= 7:
    product_type = sys.argv[6]



print(product_type)


temp_symbol_type = get_symbol_type(symbol, "NFO")



# Fallback multiplier_scale used only when wave_extractor_config.json is absent or missing the key.
# Primary source is wave_extractor_config.json — place_duo_order() reads it first.
multiplier_scale = {
    "0": [1, 1], "1": [1.3, 1], "2": [1.7, 1], "3": [2.5, 1], "4": [3, 1],
    "5": [10, 1], "6": [10, 1], "7": [10, 1], "8": [15, 1], "9": [15, 1], "10": [15, 1],
    "-1": [1, 1.3], "-2": [1, 1.7], "-3": [1, 2.5], "-4": [1, 3],
    "-5": [1, 10], "-6": [1, 10], "-7": [1, 10], "-8": [1, 15], "-9": [1, 15], "-10": [1, 15],
}

print("Symbol Type is : " + temp_symbol_type)
logging.info("Fallback multiplier_scale loaded (config file takes precedence at runtime)")


exchange = "NFO"
if symbol.startswith("SENSEX"):
    exchange = "BFO"
    set_exchange("BFO")


initilise(request_token, symbol, quantity, buy_gap, sell_gap, multiplier_scale, exchange)
set_tag("Scraper")


#Initialisations
# Optimization: No longer need to load 60k instruments into memory map.
# common_lib functions will now query SQLite as needed.

instrument_details = get_instrument_details(symbol)

days_to_expiry = instrument_details['days_to_expiry']

set_globalDeltaCalculationDays(days_to_expiry)


try:
    quote_data = get_quote_with_retry(exchange + ":" + symbol)
except Exception as e:
    logging.error("Failed to get quote after all retries: {}".format(e))
    raise

price = float(quote_data[exchange + ":" + symbol]['last_price'])

set_scraper_last_price(price)



set_typeOfProduct(product_type)

place_duo_order(symbol, product_type, True)

printCurrentStatus()

while True:
    time.sleep(180)
    reread_option_defaults()
    # 1. Immediate Market Hours Guard
    if not is_market_open():
        logging.warning("Market is closed. Exiting scraper loop.")
        sys.exit(0)
    print("Just up after 180 sec sleep")
    current_positions = {"position": get_position_for_symbol(symbol)}

    # Exit cleanly when the market has closed (15:30 IST) so we don't accidentally
    # place orders or GTTs overnight. The access token expires daily anyway, so
    # the script must be restarted fresh the next morning.
    if not is_market_open():
        logging.warning(
            "Market is closed (after 15:30 IST or weekend). "
            "Exiting wave extractor process for %s to avoid stale GTT risk.",
            symbol,
        )
        sys.exit(0)

    # Need to check if the delta values are no more restricted are orders getting created? I don't think that code is woeking currently
    #printCurrentStatus()
    try:
        check_changes_in_restrictions(symbol)
    except Exception as e:
        logging.error("Error Cancelling Order: {}".format(e))
        continue

    # Unconditionally refresh status file so Live Delta stays current in the UI
    # regardless of whether restrictions changed or orders were modified.
    try:
        write_status_to_file()
    except Exception as e:
        logging.error("Failed to write status file in main loop: %s", e)

    # Check status of existing orders (including GTTs)
    check_orders()

    is_any_order_active = check_is_any_order_active()
    if not is_any_order_active:
        # Poll for any GTT-triggered orders that may have fired while the WebSocket
        # was down or before registration. Handles reconnect + race-condition paths.
        check_gtt_orders()
        # Only place fresh duo if no GTTs are still pending (avoids duplicate orders
        # alongside a live GTT whose trigger price hasn't been hit yet).
        if not is_any_gtt_pending():
            place_duo_order(symbol, product_type, True)
