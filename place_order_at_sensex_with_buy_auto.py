###############################################################################
#
# The MIT License (MIT)
#
# Copyright (c) Zerodha Technology Pvt. Ltd.
#
# SENSEX Survivor with Auto-Buy - Trend following options trading for BSE SENSEX
# Adapted from place_order_at_nifty_with_buy_auto.py for SENSEX options on BFO
#
# Auto-buy feature: Buys hedge options 1000 points away when selling
#
###############################################################################


##TODO

## -- Issue is with the quantity, there is a multiplier while doing the transaction but it is not considered.
## -- Orders are executed on market which should be updated to 5% gap with only 1 order in pending 
## -- Need to check if the buy thing is working properly or not?



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


sensex_strike_gap_local = 100  # SENSEX options have 100-point strike intervals

min_price_to_sell = 15






sensex_pe_last_value = 0
sensex_ce_last_value = 0

# Token to subscribe once WebSocket is connected (set in main body, used in on_connect_solo)
sensex_instrument_token = None

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

symbol_initials = ""   #This is to determine which week is considered eg: SENSEX26JAN


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
    """Parse PE:CE gap string with buy gaps into dictionary."""
    return_arr = {}
    return_arr['pe'] = {}
    return_arr['ce'] = {}
    return_arr['pe']['sell'] = str.split(":")[0].split("#")[0]
    return_arr['pe']['buy'] = str.split(":")[0].split("#")[1]
    return_arr['ce']['sell'] = str.split(":")[1].split("#")[0]
    return_arr['ce']['buy'] = str.split(":")[1].split("#")[1]
    return return_arr




def return_pe_ce_gap(str):
    """
    Parse PE:CE gap string into dictionary.
    
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
    symbol_initials = sys.argv[1]


    print(len(symbol_initials))
    if len(symbol_initials) < 10:
        print("\n\n\n\n\nThe symbol is not complete. Please enter correct Symbol (e.g., SENSEX26JAN) \n\n\n\n\n")
        quit()


    symbol_gap = return_pe_ce_gap(sys.argv[2])
    pe_symbol_gap = int(symbol_gap['pe'])
    ce_symbol_gap = int(symbol_gap['ce'])

    max_price_to_buy_else_reduce = 15

    # Pass sell gap directly — find_sensex_symbol_auto_buy generates candidates at 6–16
    # strike intervals (600–1600 pts) beyond the sell strike, so pre-adding a buy gap here
    # caused double-counting and landed buys far beyond the intended 1000-pt SENSEX limit.
    pe_symbol_gap_buy = pe_symbol_gap
    ce_symbol_gap_buy = ce_symbol_gap

    exchange_temp = sys.argv[3]
    exchange = exchange_temp
    if ":" in exchange_temp:
        exchange = exchange_temp.split(":")[0]
        order_type = exchange_temp.split(":")[1]
        if order_type != "MIS":
            print("Only Order Type Supported is MIS. Quitting....")
            quit()

    set_exchange(exchange)

    gap = return_pe_ce_gap(sys.argv[4]);
    pe_gap = float(gap['pe'])
    ce_gap = float(gap['ce'])

    reset_gap = return_pe_ce_gap(sys.argv[5]);
    pe_reset_gap = float(reset_gap['pe'])
    ce_reset_gap = float(reset_gap['ce'])

    quantity = return_pe_ce_gap(sys.argv[6])
    pe_quantity = int(quantity['pe'])
    ce_quantity = int(quantity['ce'])

    start_point = return_pe_ce_gap(sys.argv[7])
    pe_start_point = int(float(start_point['pe']))
    ce_start_point = int(float(start_point['ce']))

    request_token = sys.argv[8];
    if len(sys.argv) > 9:
        override = sys.argv[9]
        if override == "True":
            over_ride_flag = 1
except Exception as e:
    logging.error("%s", e)
    logging.error("Format -- python3 place_order_at_sensex_with_buy_auto.py <SYMBOL_INITIALS> <PE_SYMBOL_GAP>:<CE_SYMBOL_GAP> <EXCHANGE> <PE GAP To Sell After>:<CE GAP To Sell After> <PE Reset Gap>:<CE Reset Gap> <PE QUANTITY>:<CE QUANTITY> <PE_START_POINT>:<CE_START_POINT> <REQUEST_TOKEN>")
    logging.error("Sample-- python3 place_order_at_sensex_with_buy_auto.py SENSEX26JAN 500:600 BFO 100:100 200:200 20:20 0:0 aa")
    quit()

start_number = number
current_number = number

pe_current_gap = pe_gap
ce_current_gap = ce_gap


temp_trans_type = trans_type

if trans_type == "BUY" :
    trans_type = kite.TRANSACTION_TYPE_BUY
elif trans_type == "SELL" :
    trans_type = kite.TRANSACTION_TYPE_SELL
else:
    print("Trans Type Does not seem to be correct. Format is BUY/SELL/GTT:BUY/GTT:SELL ... Quitting....")
    quit()

if buy_trans_type == "BUY" :
    buy_trans_type = kite.TRANSACTION_TYPE_BUY
elif buy_trans_type == "SELL" :
    buy_trans_type = kite.TRANSACTION_TYPE_SELL
else:
    print("Trans Type Does not seem to be correct. Format is BUY/SELL/GTT:BUY/GTT:SELL ... Quitting....")
    quit()



if order_type == "":
    if exchange == "BFO":
        order_type = "NRML"

    if exchange == "BSE":
        order_type = "CNC"




def place_order_at_market_for_faroff_symbols(symbol, trans_type, exchange, order_type, quantity, instrument_details=None):
    """Place limit order at near-market price for far OTM symbols."""
    current_orders = kite.orders()
    print("================================================================---")
    count = 0
    for order in current_orders:
        current_symbol = order['tradingsymbol']
        if symbol == current_symbol:
            print(symbol+" Order present "+order['transaction_type']+" - Quantity - "+str(order['quantity'])+" - Price - "+str(order['price']))
            count = count+1
    print("================================================================---")
    quote_data = kite.quote(exchange+":"+symbol)
    price = float(quote_data[exchange+":"+symbol]['last_price'])

    variety = kite.VARIETY_REGULAR 


    if trans_type == kite.TRANSACTION_TYPE_BUY:
        price = price + 0.1
    else:
        price = price - 0.1


    print("Putting LIMIT order 10 paise off Market price "+ str(price))
    order_id = place_order(symbol, variety, trans_type, exchange, order_type, price, quantity)
    
    # Extract instrument_token and segment from instrument_details if available
    inst_token = instrument_details.get('instrument_token') if instrument_details else None
    inst_segment = instrument_details.get('segment') if instrument_details else None
    add_order_to_list(order_id, price, quantity, trans_type, symbol, "-1", instrument_token=inst_token, segment=inst_segment)
    return order_id


def place_order_at_market(symbol, trans_type, exchange, order_type, quantity, instrument_details=None):
    """Place market order for the given symbol."""
    current_orders = kite.orders()
    print("================================================================---")
    count = 0
    for order in current_orders:
        current_symbol = order['tradingsymbol']
        if symbol == current_symbol:
            print(symbol+" Order present "+order['transaction_type']+" - Quantity - "+str(order['quantity'])+" - Price - "+str(order['price']))
            count = count+1
    print("================================================================---")
    quote_data = kite.quote(exchange+":"+symbol)
    price = float(quote_data[exchange+":"+symbol]['last_price']) 

    variety = kite.VARIETY_REGULAR 

    order_id = place_order_market(symbol, variety, trans_type, exchange, order_type, quantity)
    
    # Extract instrument_token and segment from instrument_details if available
    inst_token = instrument_details.get('instrument_token') if instrument_details else None
    inst_segment = instrument_details.get('segment') if instrument_details else None
    add_order_to_list(order_id, price, quantity, trans_type, symbol, "-1", instrument_token=inst_token, segment=inst_segment)
    return order_id


def check_times_to_sell_under_control(times_to_sell):
    """Check if number of orders to place is reasonable, prompt if too many."""
    if times_to_sell < 5:
        return True

    while True:
        continue_or_not = input("\n\n\n\n\n\nTimes to sell multiple > or equal to 5. Do you want to continue Yes/No: ")
        if not continue_or_not :
            continue
        if continue_or_not == "No" :
            return False
        if continue_or_not == "Yes" :
            return True


def on_ticks_update(ws, ticks):  # noqa
    """
    Callback to receive ticks and execute orders based on SENSEX price movement.
    
    Opens spread positions:
    - Sells PE options when SENSEX moves up by pe_gap points
    - Buys hedge PE options 1000 points further OTM
    - Sells CE options when SENSEX moves down by ce_gap points
    - Buys hedge CE options 1000 points further OTM
    """
    update_last_tick_time()
    global sensex_pe_last_value
    global sensex_ce_last_value

    global trans_type
    global exchange
    global order_type

    global sensex_strike_gap_local
    global min_price_to_sell


    global pe_reset_gap_flag
    global ce_reset_gap_flag

    global pe_reset_gap
    global ce_reset_gap

    if sensex_pe_last_value == 0 or sensex_ce_last_value == 0:
        return


    sensex_current_val = ticks[0]['last_price']
    write_spot_price("SENSEX", sensex_current_val)
    if sensex_current_val > sensex_pe_last_value:
        if (sensex_current_val - sensex_pe_last_value) > pe_gap: 
            times_to_sell = int((sensex_current_val - sensex_pe_last_value)/pe_gap)
            if check_times_to_sell_under_control(times_to_sell):
                sensex_pe_last_value = sensex_pe_last_value + pe_gap*times_to_sell
                _write_survivor_state(sensex_pe_last_value, sensex_ce_last_value)
                set_trigger_spot_price(sensex_current_val, "SENSEX")
                quantity_to_sell = times_to_sell*pe_quantity
                quantity_to_buy = quantity_to_sell

                temp_pe_symbol_gap = pe_symbol_gap
                temp_pe_symbol_gap_buy = pe_symbol_gap_buy
                return_instrument_details = None
                return_instrument_details_buy = None
                while True:
                    return_instrument_details = find_sensex_symbol_from_gap(symbol_initials, temp_pe_symbol_gap, "PE")
                    if return_instrument_details is None:
                        logging.error("Could not find PE sell symbol for {} {}, skipping this tick".format(symbol_initials, temp_pe_symbol_gap))
                        break
                    sell_instrument_symbol = return_instrument_details['tradingsymbol']
                    return_instrument_details_buy = find_sensex_symbol_auto_buy(symbol_initials, temp_pe_symbol_gap_buy, "PE", max_price_to_buy_else_reduce, sell_instrument_symbol)
                    if return_instrument_details_buy is None:
                        logging.error("Could not find PE buy symbol for {} {}, skipping this tick".format(symbol_initials, temp_pe_symbol_gap_buy))
                        break
                    logging.error("Symbol to Sell {}".format(return_instrument_details));
                    logging.error("Execute PE SELL order "+str(temp_pe_symbol_gap)+" Symbol = "+return_instrument_details['tradingsymbol']+" Quantity = "+str(quantity_to_sell)+" at market price pe_last_value = "+str(sensex_pe_last_value));
                    logging.error("Execute PE BUY order Symbol = "+return_instrument_details_buy['tradingsymbol']+" Quantity = "+str(quantity_to_buy)+" at market price pe_last_value = "+str(sensex_pe_last_value));
                    quote_data = kite.quote(exchange+":"+return_instrument_details['tradingsymbol'])
                    price = float(quote_data[exchange+":"+return_instrument_details['tradingsymbol']]['last_price'])
                    if price < min_price_to_sell:
                        temp_pe_symbol_gap = temp_pe_symbol_gap - sensex_strike_gap_local
                    else:
                        break

                if return_instrument_details is None or return_instrument_details_buy is None:
                    logging.error("PE symbol not found, skipping order placement")
                else:
                    place_order_at_market(return_instrument_details_buy['tradingsymbol'], buy_trans_type, exchange, order_type, pe_quantity, return_instrument_details_buy)
                    time.sleep(1)
                    place_order_at_market(return_instrument_details['tradingsymbol'], trans_type, exchange, order_type, pe_quantity, return_instrument_details)
                    pe_reset_gap_flag = 1
            else:
                logging.error("Instructed not to continue ahead with PE trades")
        else:
            logging.error(symbol_initials + " SENSEX still under control. Doing Nothing -- Time = "+get_ist_now().strftime('%H:%M:%S')+" -- pe_value = "+str(sensex_pe_last_value)+" -- CE value = "+str(sensex_ce_last_value)+" Current SENSEX Value = "+str(sensex_current_val) )


    if sensex_current_val < sensex_ce_last_value:
        if (sensex_ce_last_value - sensex_current_val) > ce_gap:
            times_to_sell = int((sensex_ce_last_value - sensex_current_val)/ce_gap)
            if check_times_to_sell_under_control(times_to_sell):
                sensex_ce_last_value = sensex_ce_last_value - ce_gap*times_to_sell
                _write_survivor_state(sensex_pe_last_value, sensex_ce_last_value)
                set_trigger_spot_price(sensex_current_val, "SENSEX")
                quantity_to_sell = times_to_sell*ce_quantity
                quantity_to_buy = quantity_to_sell


                temp_ce_symbol_gap = ce_symbol_gap
                temp_ce_symbol_gap_buy = ce_symbol_gap_buy
                return_instrument_details = None
                return_instrument_details_buy = None

                while True:
                    return_instrument_details = find_sensex_symbol_from_gap(symbol_initials, temp_ce_symbol_gap, "CE")
                    if return_instrument_details is None:
                        logging.error("Could not find CE sell symbol for {} {}, skipping this tick".format(symbol_initials, temp_ce_symbol_gap))
                        break
                    sell_instrument_symbol = return_instrument_details['tradingsymbol']
                    return_instrument_details_buy = find_sensex_symbol_auto_buy(symbol_initials, temp_ce_symbol_gap_buy, "CE", max_price_to_buy_else_reduce, sell_instrument_symbol)
                    if return_instrument_details_buy is None:
                        logging.error("Could not find CE buy symbol for {} {}, skipping this tick".format(symbol_initials, temp_ce_symbol_gap_buy))
                        break
                    logging.error("Execute CE SELL order "+str(temp_ce_symbol_gap)+" Symbol = "+return_instrument_details['tradingsymbol']+" Quantity = "+str(quantity_to_sell)+" at market price ce_last_value = "+str(sensex_ce_last_value));
                    logging.error("Execute CE BUY order Symbol = "+return_instrument_details_buy['tradingsymbol']+" Quantity = "+str(quantity_to_buy)+" at market price ce_last_value = "+str(sensex_ce_last_value));
                    quote_data = kite.quote(exchange+":"+return_instrument_details['tradingsymbol'])
                    price = float(quote_data[exchange+":"+return_instrument_details['tradingsymbol']]['last_price'])
                    if price < min_price_to_sell:
                        temp_ce_symbol_gap = temp_ce_symbol_gap - sensex_strike_gap_local
                    else:
                        break

                if return_instrument_details is None or return_instrument_details_buy is None:
                    logging.error("CE symbol not found, skipping order placement")
                else:
                    place_order_at_market(return_instrument_details_buy['tradingsymbol'], buy_trans_type, exchange, order_type, ce_quantity, return_instrument_details_buy)
                    time.sleep(1)
                    place_order_at_market(return_instrument_details['tradingsymbol'], trans_type, exchange, order_type, ce_quantity, return_instrument_details)
                    ce_reset_gap_flag = 1
            else:
                logging.error("Instructed not to continue ahead with CE trades")

        else:
            logging.error(symbol_initials + " SENSEX still under control. Doing Nothing -- Time = "+get_ist_now().strftime('%H:%M:%S')+" -- pe_value = "+str(sensex_pe_last_value)+" -- CE value = "+str(sensex_ce_last_value)+" Current SENSEX Value = "+str(sensex_current_val)+" Checking with CE gap of "+str(ce_gap))
        
    logging.error(symbol_initials + " SENSEX still under control. Doing Nothing -- Time = "+get_ist_now().strftime('%H:%M:%S')+" -- pe_value = "+str(sensex_pe_last_value)+" -- CE value = "+str(sensex_ce_last_value)+" Current SENSEX Value = "+str(sensex_current_val)+" Checking with CE gap of "+str(ce_gap))

    #This code is to reset the pe and ce current values
    if (sensex_pe_last_value - sensex_current_val) > pe_reset_gap and pe_reset_gap_flag == 1:
        logging.error("Old PE value is "+str(sensex_pe_last_value));
        sensex_pe_last_value = sensex_current_val + pe_reset_gap
        _write_survivor_state(sensex_pe_last_value, sensex_ce_last_value)
        logging.error("Setting PE value as "+str(sensex_pe_last_value));

    if (sensex_current_val - sensex_ce_last_value) > ce_reset_gap and ce_reset_gap_flag == 1:
        logging.error("Old CE value is "+str(sensex_ce_last_value));
        sensex_ce_last_value = sensex_current_val - ce_reset_gap
        _write_survivor_state(sensex_pe_last_value, sensex_ce_last_value)
        logging.error("Setting CE value as "+str(sensex_ce_last_value));


def on_connect_solo(ws, response):  # noqa
    """Callback on successful WebSocket connect.

    Subscribes to the sensex instrument token here (not in the main thread)
    to avoid calling sendMessage before the connection is open.
    Also checks on any pending orders on reconnect (mirrors common_lib behavior).
    """
    global sensex_instrument_token
    logging.error("WebSocket connected (on_connect_solo)")

    # Check pending orders on reconnect (same as common_lib.on_connect_solo)
    orders = get_orders()
    logging.info("Current Solo Order Conditions {}".format(orders))
    if len(orders) > 0:
        check_orders_solo()

    # Subscribe to the instrument token — must happen inside on_connect, not in
    # the main thread, because kws.ws is None until the handshake completes.
    if sensex_instrument_token is not None:
        subscribe_for_tick([sensex_instrument_token])
        logging.error("Subscribed to instrument token: {}".format(sensex_instrument_token))
    else:
        logging.error("WARNING: sensex_instrument_token is None in on_connect_solo")


def on_order_update_solo_callback(order_id, order_update_status):
    """Callback for order status updates."""
    print("Solo Call Back is called")

    global current_number
    global global_multiplier
    global global_multiplier_factor

    if order_update_status == "complete":
        current_number = current_number-1 
    elif order_update_status == "cancelled":
        print("Someone cancelled the order, can be because of time up as well.")
    else:
        print("Order was updated from some other place") 






initilise_basic(request_token, on_ticks_update, on_connect_solo, on_order_update_solo)
set_tag("Trend_Mkt_SENSEX")

set_on_order_update_solo_callback(on_order_update_solo_callback)




sensex_value = get_sensex_current_quote()

if pe_start_point == 0:
    sensex_pe_last_value = sensex_value['last_price']
else:
    sensex_pe_last_value = pe_start_point

if ce_start_point == 0:
    sensex_ce_last_value = sensex_value['last_price']
else:
    sensex_ce_last_value = ce_start_point




_write_survivor_state(sensex_pe_last_value, sensex_ce_last_value)
logging.error("---------- SENSEX PE Start Value  : {}".format(sensex_pe_last_value))
logging.error("---------- SENSEX CE Start Value  : {}".format(sensex_ce_last_value))




get_all_fut_opt_instruments()

# Store token so on_connect_solo can subscribe once the WebSocket is open.
sensex_instrument_token = sensex_value['instrument_token']
logging.error("Instrument token stored for subscription: {}".format(sensex_instrument_token))

# Race-condition fix: attempt subscription now. subscribe_for_tick() uses
# common_lib.kws (the live KiteTicker), so it succeeds if already connected.
# If not yet connected it raises; on_connect_solo will subscribe on connect.
try:
    subscribe_for_tick([sensex_instrument_token])
    logging.error(
        "WebSocket subscription successful (direct path) for token: %d — "
        "ticks will start arriving",
        sensex_instrument_token,
    )
except Exception as _subscribe_error:
    logging.info(
        "WebSocket not yet connected (token %d stored); on_connect_solo will "
        "subscribe when connection completes. (error: %s)",
        sensex_instrument_token,
        _subscribe_error,
    )

logging.info("Waiting for WebSocket connection to subscribe...")


watchdog_sleep()




logging.error("single leg gtt order trigger_id : {}".format(sensex_value)) 
quit()

