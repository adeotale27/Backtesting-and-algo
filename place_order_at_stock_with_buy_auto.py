###############################################################################
#
# The MIT License (MIT)
#
# Copyright (c) Zerodha Technology Pvt. Ltd.
#
# Generic NFO Stock Survivor with Auto-Buy - Trend following options trading
# for any NFO-listed stock (e.g., TCS, RELIANCE, INFY, HDFCBANK).
#
# Buy+Sell spread mode: Sells options and buys hedge options further OTM.
#
# Usage:
#   python3 place_order_at_stock_with_buy_auto.py <STOCK_NAME>
#       <SYMBOL_INITIALS> <PE_DIST:CE_DIST> <EXCHANGE> <PE_GAP:CE_GAP>
#       <PE_RESET:CE_RESET> <PE_QTY:CE_QTY> <PE_START:CE_START>
#       <REQUEST_TOKEN>
#
# Example:
#   python3 place_order_at_stock_with_buy_auto.py TCS TCS26FEB 50:50
#       NFO 5:5 10:10 175:175 0:0 <token>
###############################################################################


import sys
import os
import datetime
import json
from common_lib import *


_SURVIVOR_STATE_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), '..', 'survivor_status')


def _write_survivor_state(pe_value: float, ce_value: float) -> None:
    """Write current PE/CE tracking values to state file for dashboard visibility.

    Args:
        pe_value: Current PE tracking value.
        ce_value: Current CE tracking value.
    """
    state = {
        "pe_value": pe_value,
        "ce_value": ce_value,
        "updated": get_ist_now().strftime("%Y-%m-%d %H:%M:%S"),
    }
    state_path = os.path.join(_SURVIVOR_STATE_DIR, f"survivor_state_{os.getpid()}.json")
    try:
        with open(state_path, 'w') as f:
            json.dump(state, f)
    except OSError as e:
        logging.error(f"Failed to write survivor state: {e}")


global_multiplier = 1
global_multiplier_factor = 1.2


min_price_to_sell = 15


stock_pe_last_value = 0
stock_ce_last_value = 0

# Token to subscribe once WebSocket is connected (set in main body, used in on_connect_solo)
stock_instrument_token = None

start_mins_per_order_gap = 0
current_mins_per_order_gap = 0
last_mins_per_order_gap = 0
max_mins_left = 0

waiting_counter_secs = 0


order_type = ""
exchange = ""

number = 0
start_number = 0
current_number = 0


last_order_executed_time = {}
last_order_executed_time['mins'] = 0
last_order_executed_time['hours'] = 0


gap = 0
start_gap = 0
current_gap = 0

symbol = ""

stock_name = ""  # e.g., TCS, RELIANCE
symbol_initials = ""  # e.g., TCS26FEB

symbol_gap = ""
pe_symbol_gap = 0
ce_symbol_gap = 0


gap = ""
pe_gap = 0
ce_gap = 0

reset_gap = ""
pe_reset_gap = 0
ce_reset_gap = 0
pe_reset_gap_flag = 0
ce_reset_gap_flag = 0


symbol_type = ""
trans_type = "SELL"
buy_trans_type = "BUY"
over_ride_flag = 0

quantity = ""
pe_quantity = 0
ce_quantity = 0


def return_pe_ce_gap_with_buy(str):
    """Parse PE:CE gap string with buy gaps into dictionary.

    Args:
        str: Colon-separated string like "200:300"

    Returns:
        dict: {'pe': '200', 'ce': '300'}
    """
    return_arr = {}
    return_arr['pe'] = str.split(":")[0]
    return_arr['ce'] = str.split(":")[1]
    return return_arr


def return_pe_ce_gap(str):
    """Parse PE:CE gap string into dictionary.

    Args:
        str: Colon-separated string like "200:300"

    Returns:
        dict: {'pe': '200', 'ce': '300'}
    """
    return_arr = {}
    return_arr['pe'] = str.split(":")[0]
    return_arr['ce'] = str.split(":")[1]
    return return_arr


try:
    stock_name = sys.argv[1]  # e.g., TCS, RELIANCE

    symbol_initials = sys.argv[2]  # e.g., TCS26FEB

    print(len(symbol_initials))
    if len(symbol_initials) < 5:
        print("\n\n\n\n\nThe symbol is not complete. "
              "Please enter correct Symbol \n\n\n\n\n")
        quit()

    symbol_gap = return_pe_ce_gap(sys.argv[3])
    pe_symbol_gap = int(symbol_gap['pe'])
    ce_symbol_gap = int(symbol_gap['ce'])

    exchange_temp = sys.argv[4]
    exchange = exchange_temp
    if ":" in exchange_temp:
        exchange = exchange_temp.split(":")[0]
        order_type = exchange_temp.split(":")[1]
        if order_type != "MIS":
            print("Only Order Type Supported is MIS. Quitting....")
            quit()

    gap = return_pe_ce_gap(sys.argv[5])
    pe_gap = float(gap['pe'])
    ce_gap = float(gap['ce'])

    reset_gap = return_pe_ce_gap(sys.argv[6])
    pe_reset_gap = float(reset_gap['pe'])
    ce_reset_gap = float(reset_gap['ce'])

    quantity = return_pe_ce_gap(sys.argv[7])
    pe_quantity = int(quantity['pe'])
    ce_quantity = int(quantity['ce'])

    start_point = return_pe_ce_gap(sys.argv[8])
    pe_start_point = int(float(start_point['pe']))
    ce_start_point = int(float(start_point['ce']))

    request_token = sys.argv[9]
    if len(sys.argv) > 10:
        override = sys.argv[10]
        if override == "True":
            over_ride_flag = 1
except Exception as e:
    logging.error("%s", e)
    logging.error(
        "Format -- python3 place_order_at_stock_with_buy_auto.py "
        "<STOCK_NAME> <SYMBOL_INITIALS> <PE_DIST:CE_DIST> <EXCHANGE> "
        "<PE_GAP:CE_GAP> <PE_RESET:CE_RESET> "
        "<PE_QTY:CE_QTY> <PE_START:CE_START> <REQUEST_TOKEN>"
    )
    logging.error(
        "Sample-- python3 place_order_at_stock_with_buy_auto.py TCS "
        "TCS26FEB 50:50 NFO 5:5 10:10 175:175 0:0 token123"
    )
    quit()

start_number = number
current_number = number

pe_current_gap = pe_gap
ce_current_gap = ce_gap


temp_trans_type = trans_type

if trans_type == "BUY":
    trans_type = kite.TRANSACTION_TYPE_BUY
elif trans_type == "SELL":
    trans_type = kite.TRANSACTION_TYPE_SELL
else:
    print("Trans Type Does not seem to be correct. "
          "Format is BUY/SELL/GTT:BUY/GTT:SELL ... Quitting....")
    quit()

buy_trans_type = kite.TRANSACTION_TYPE_BUY

if order_type == "":
    if exchange == "NFO":
        order_type = "NRML"

    if exchange == "NSE":
        order_type = "CNC"


def place_order_at_market_for_faroff_symbols(
    symbol, trans_type, exchange, order_type, quantity,
    instrument_details=None
):
    """Place limit order at near-market price for far OTM symbols.

    Args:
        symbol: Trading symbol string.
        trans_type: Kite transaction type constant.
        exchange: Exchange string (e.g., 'NFO').
        order_type: Order type string (e.g., 'NRML').
        quantity: Number of shares/lots.
        instrument_details: Optional instrument metadata dict.

    Returns:
        str: Order ID from Kite.
    """
    current_orders = kite.orders()
    print("================================================================---")
    count = 0
    for order in current_orders:
        current_symbol = order['tradingsymbol']
        if symbol == current_symbol:
            print(symbol + " Order present " + order['transaction_type']
                  + " - Quantity - " + str(order['quantity'])
                  + " - Price - " + str(order['price']))
            count = count + 1
    print("================================================================---")
    quote_data = kite.quote(exchange + ":" + symbol)
    price = float(quote_data[exchange + ":" + symbol]['last_price'])

    variety = kite.VARIETY_REGULAR

    if trans_type == kite.TRANSACTION_TYPE_BUY:
        price = price + 0.1
    else:
        price = price - 0.1

    print("Putting LIMIT order 10 paise off Market price " + str(price))
    order_id = place_order(
        symbol, variety, trans_type, exchange, order_type, price, quantity
    )
    add_order_to_list(order_id, price, quantity, trans_type, symbol, "-1")
    return order_id


def place_order_at_market(
    symbol, trans_type, exchange, order_type, quantity,
    instrument_details=None
):
    """Place market order for the given symbol.

    Args:
        symbol: Trading symbol string.
        trans_type: Kite transaction type constant.
        exchange: Exchange string (e.g., 'NFO').
        order_type: Order type string (e.g., 'NRML').
        quantity: Number of shares/lots.
        instrument_details: Optional instrument metadata dict.

    Returns:
        str: Order ID from Kite.
    """
    current_orders = kite.orders()
    print("================================================================---")
    count = 0
    for order in current_orders:
        current_symbol = order['tradingsymbol']
        if symbol == current_symbol:
            print(symbol + " Order present " + order['transaction_type']
                  + " - Quantity - " + str(order['quantity'])
                  + " - Price - " + str(order['price']))
            count = count + 1
    print("================================================================---")
    quote_data = kite.quote(exchange + ":" + symbol)
    price = float(quote_data[exchange + ":" + symbol]['last_price'])

    variety = kite.VARIETY_REGULAR

    order_id = place_order_market(
        symbol, variety, trans_type, exchange, order_type, quantity
    )
    add_order_to_list(order_id, price, quantity, trans_type, symbol, "-1")
    return order_id


def check_times_to_sell_under_control(times_to_sell):
    """Check if number of orders to place is reasonable, prompt if too many.

    Args:
        times_to_sell: Number of order multiples triggered.

    Returns:
        bool: True if safe to proceed, False otherwise.
    """
    if times_to_sell < 5:
        return True

    while True:
        continue_or_not = input(
            "\n\n\n\n\n\nTimes to sell multiple > or equal to 5. "
            "Do you want to continue Yes/No: "
        )
        if not continue_or_not:
            continue
        if continue_or_not == "No":
            return False
        if continue_or_not == "Yes":
            return True


def on_ticks_update(ws, ticks):  # noqa
    """Callback for stock price ticks - opens spread positions.

    Opens spread positions:
    - Sells PE options when stock moves up by pe_gap points
    - Buys hedge PE options further OTM
    - Sells CE options when stock moves down by ce_gap points
    - Buys hedge CE options further OTM

    Args:
        ws: WebSocket connection object.
        ticks: List of tick data dictionaries.
    """
    update_last_tick_time()
    global stock_pe_last_value
    global stock_ce_last_value

    global trans_type
    global buy_trans_type
    global exchange
    global order_type

    global min_price_to_sell

    global pe_reset_gap_flag
    global ce_reset_gap_flag

    global pe_reset_gap
    global ce_reset_gap

    if stock_pe_last_value == 0 or stock_ce_last_value == 0:
        return

    stock_current_val = ticks[0]['last_price']
    write_spot_price(stock_name, stock_current_val)

    # Get auto-detected strike gap for this stock
    stock_strike_gap = get_stock_strike_gap(stock_name)

    # Buy hedge distance: use the larger of symbol_gap or 10 * strike_gap
    pe_buy_hedge_distance = pe_symbol_gap + max(
        stock_strike_gap * 10, pe_symbol_gap
    )
    ce_buy_hedge_distance = ce_symbol_gap + max(
        stock_strike_gap * 10, ce_symbol_gap
    )

    if stock_current_val > stock_pe_last_value:
        if (stock_current_val - stock_pe_last_value) > pe_gap:
            times_to_sell = int(
                (stock_current_val - stock_pe_last_value) / pe_gap
            )
            if check_times_to_sell_under_control(times_to_sell):
                stock_pe_last_value = (
                    stock_pe_last_value + pe_gap * times_to_sell
                )
                _write_survivor_state(stock_pe_last_value, stock_ce_last_value)
                set_trigger_spot_price(stock_current_val, stock_name)
                quantity_to_sell = times_to_sell * pe_quantity

                temp_pe_symbol_gap = pe_symbol_gap
                while True:
                    return_instrument_details = find_stock_symbol_from_gap(
                        stock_name, symbol_initials,
                        temp_pe_symbol_gap, "PE"
                    )
                    if return_instrument_details is None:
                        logging.error(
                            "Could not find PE symbol, skipping this tick"
                        )
                        break
                    logging.error(
                        "Symbol to Sell {}".format(return_instrument_details)
                    )
                    logging.error(
                        "Execute PE sell order Far off "
                        + str(temp_pe_symbol_gap) + " Symbol = "
                        + return_instrument_details['tradingsymbol']
                        + " Quantity = " + str(quantity_to_sell)
                        + " at market price pe_last_value = "
                        + str(stock_pe_last_value)
                    )
                    quote_data = kite.quote(
                        exchange + ":"
                        + return_instrument_details['tradingsymbol']
                    )
                    price = float(
                        quote_data[
                            exchange + ":"
                            + return_instrument_details['tradingsymbol']
                        ]['last_price']
                    )
                    if price < min_price_to_sell:
                        temp_pe_symbol_gap = (
                            temp_pe_symbol_gap - stock_strike_gap
                        )
                    else:
                        break

                if return_instrument_details is None:
                    logging.error(
                        "Symbol not found, skipping order placement"
                    )
                else:
                    # Sell PE
                    place_order_at_market(
                        return_instrument_details['tradingsymbol'],
                        trans_type, exchange, order_type, pe_quantity
                    )

                    # Buy hedge PE (further OTM)
                    buy_instrument = find_stock_symbol_auto_buy(
                        stock_name, symbol_initials,
                        pe_buy_hedge_distance, "PE",
                        min_price_to_sell,
                        return_instrument_details['tradingsymbol']
                    )
                    if buy_instrument is not None:
                        logging.error(
                            "Buying hedge PE: {}".format(
                                buy_instrument['tradingsymbol']
                            )
                        )
                        place_order_at_market(
                            buy_instrument['tradingsymbol'],
                            buy_trans_type, exchange,
                            order_type, pe_quantity
                        )
                    else:
                        logging.error(
                            "Could not find PE hedge symbol"
                        )

                    pe_reset_gap_flag = 1
            else:
                logging.error(
                    "Instructed not to continue ahead with PE trades"
                )
        else:
            logging.error(
                symbol_initials + " " + stock_name
                + " still under control. Doing Nothing -- Time = "
                + get_ist_now().strftime('%H:%M:%S')
                + " -- pe_value = " + str(stock_pe_last_value)
                + " -- CE value = " + str(stock_ce_last_value)
                + " Current Value = " + str(stock_current_val)
            )

    if stock_current_val < stock_ce_last_value:
        if (stock_ce_last_value - stock_current_val) > ce_gap:
            times_to_sell = int(
                (stock_ce_last_value - stock_current_val) / ce_gap
            )
            if check_times_to_sell_under_control(times_to_sell):
                stock_ce_last_value = (
                    stock_ce_last_value - ce_gap * times_to_sell
                )
                _write_survivor_state(stock_pe_last_value, stock_ce_last_value)
                set_trigger_spot_price(stock_current_val, stock_name)
                quantity_to_sell = times_to_sell * ce_quantity

                temp_ce_symbol_gap = ce_symbol_gap

                while True:
                    return_instrument_details = find_stock_symbol_from_gap(
                        stock_name, symbol_initials,
                        temp_ce_symbol_gap, "CE"
                    )
                    if return_instrument_details is None:
                        logging.error(
                            "Could not find CE symbol, skipping this tick"
                        )
                        break
                    logging.error(
                        "Execute CE sell order Far off "
                        + str(temp_ce_symbol_gap) + " Symbol = "
                        + return_instrument_details['tradingsymbol']
                        + " Quantity = " + str(quantity_to_sell)
                        + " at market price ce_last_value = "
                        + str(stock_ce_last_value)
                    )
                    quote_data = kite.quote(
                        exchange + ":"
                        + return_instrument_details['tradingsymbol']
                    )
                    price = float(
                        quote_data[
                            exchange + ":"
                            + return_instrument_details['tradingsymbol']
                        ]['last_price']
                    )
                    if price < min_price_to_sell:
                        temp_ce_symbol_gap = (
                            temp_ce_symbol_gap - stock_strike_gap
                        )
                    else:
                        break

                if return_instrument_details is None:
                    logging.error(
                        "Symbol not found, skipping order placement"
                    )
                else:
                    # Sell CE
                    place_order_at_market(
                        return_instrument_details['tradingsymbol'],
                        trans_type, exchange, order_type, ce_quantity
                    )

                    # Buy hedge CE (further OTM)
                    buy_instrument = find_stock_symbol_auto_buy(
                        stock_name, symbol_initials,
                        ce_buy_hedge_distance, "CE",
                        min_price_to_sell,
                        return_instrument_details['tradingsymbol']
                    )
                    if buy_instrument is not None:
                        logging.error(
                            "Buying hedge CE: {}".format(
                                buy_instrument['tradingsymbol']
                            )
                        )
                        place_order_at_market(
                            buy_instrument['tradingsymbol'],
                            buy_trans_type, exchange,
                            order_type, ce_quantity
                        )
                    else:
                        logging.error(
                            "Could not find CE hedge symbol"
                        )

                    ce_reset_gap_flag = 1
            else:
                logging.error(
                    "Instructed not to continue ahead with CE trades"
                )

        else:
            logging.error(
                symbol_initials + " " + stock_name
                + " still under control. Doing Nothing -- Time = "
                + get_ist_now().strftime('%H:%M:%S')
                + " -- pe_value = " + str(stock_pe_last_value)
                + " -- CE value = " + str(stock_ce_last_value)
                + " Current Value = " + str(stock_current_val)
            )

    logging.error(
        symbol_initials + " " + stock_name
        + " still under control. Doing Nothing -- Time = "
        + get_ist_now().strftime('%H:%M:%S')
        + " -- pe_value = " + str(stock_pe_last_value)
        + " -- CE value = " + str(stock_ce_last_value)
        + " Current Value = " + str(stock_current_val)
        + " Checking with CE gap of " + str(ce_gap)
    )

    # Reset pe and ce tracking values when price reverses significantly
    if ((stock_pe_last_value - stock_current_val) > pe_reset_gap
            and pe_reset_gap_flag == 1):
        logging.error("Old PE value is " + str(stock_pe_last_value))
        stock_pe_last_value = stock_current_val + pe_reset_gap
        _write_survivor_state(stock_pe_last_value, stock_ce_last_value)
        logging.error("Setting PE value as " + str(stock_pe_last_value))

    if ((stock_current_val - stock_ce_last_value) > ce_reset_gap
            and ce_reset_gap_flag == 1):
        logging.error("Old CE value is " + str(stock_ce_last_value))
        stock_ce_last_value = stock_current_val - ce_reset_gap
        _write_survivor_state(stock_pe_last_value, stock_ce_last_value)
        logging.error("Setting CE value as " + str(stock_ce_last_value))


def on_connect_solo(ws, response):  # noqa
    """Callback on successful WebSocket connect.

    Subscribes to the stock instrument token here (not in the main thread)
    to avoid calling sendMessage before the connection is open.
    Also checks on any pending orders on reconnect (mirrors common_lib behavior).
    """
    global stock_instrument_token
    logging.error("WebSocket connected (on_connect_solo)")

    # Check pending orders on reconnect (same as common_lib.on_connect_solo)
    orders = get_orders()
    logging.info("Current Solo Order Conditions {}".format(orders))
    if len(orders) > 0:
        check_orders_solo()

    # Subscribe to the instrument token — must happen inside on_connect, not in
    # the main thread, because kws.ws is None until the handshake completes.
    if stock_instrument_token is not None:
        subscribe_for_tick([stock_instrument_token])
        logging.error("Subscribed to instrument token: {}".format(stock_instrument_token))
    else:
        logging.error("WARNING: stock_instrument_token is None in on_connect_solo")


def on_order_update_solo_callback(order_id, order_update_status):
    """Callback for order status updates.

    Args:
        order_id: The order ID from Kite.
        order_update_status: Status string ('complete', 'cancelled', or other).
    """
    print("Solo Call Back is called")

    global current_number
    global global_multiplier
    global global_multiplier_factor

    if order_update_status == "complete":
        current_number = current_number - 1
    elif order_update_status == "cancelled":
        print("Someone cancelled the order, "
              "can be because of time up as well.")
    else:
        print("Order was updated from some other place")


# ============================================================================
# Initialization
# ============================================================================

initilise_basic(
    request_token, on_ticks_update,
    on_connect_solo, on_order_update_solo
)

# Truncate tag to 20 chars (Kite max tag length)
algo_tag = f"Trend_Mkt_{stock_name}"[:20]
set_tag(algo_tag)

set_on_order_update_solo_callback(on_order_update_solo_callback)


# Get current stock price
stock_value = get_stock_current_quote(stock_name)

if pe_start_point == 0:
    stock_pe_last_value = stock_value['last_price']
else:
    stock_pe_last_value = pe_start_point

if ce_start_point == 0:
    stock_ce_last_value = stock_value['last_price']
else:
    stock_ce_last_value = ce_start_point


_write_survivor_state(stock_pe_last_value, stock_ce_last_value)
logging.error(
    "---------- {} PE Start Value  : {}".format(
        stock_name, stock_pe_last_value
    )
)
logging.error(
    "---------- {} CE Start Value  : {}".format(
        stock_name, stock_ce_last_value
    )
)


get_all_fut_opt_instruments()

# Store token so on_connect_solo can subscribe once the WebSocket is open.
stock_instrument_token = stock_value['instrument_token']
logging.error("Instrument token stored for subscription: {}".format(stock_instrument_token))

# Race-condition fix: attempt subscription now. subscribe_for_tick() uses
# common_lib.kws (the live KiteTicker), so it succeeds if already connected.
# If not yet connected it raises; on_connect_solo will subscribe on connect.
try:
    subscribe_for_tick([stock_instrument_token])
    logging.error(
        "WebSocket subscription successful (direct path) for token: %d — "
        "ticks will start arriving",
        stock_instrument_token,
    )
except Exception as _subscribe_error:
    logging.info(
        "WebSocket not yet connected (token %d stored); on_connect_solo will "
        "subscribe when connection completes. (error: %s)",
        stock_instrument_token,
        _subscribe_error,
    )

logging.info("Waiting for WebSocket connection to subscribe...")


watchdog_sleep()


logging.error(
    "single leg gtt order trigger_id : {}".format(stock_value)
)
quit()
