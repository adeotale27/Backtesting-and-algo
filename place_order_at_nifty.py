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
import datetime
import logging
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


# nifty_lot_size is imported from common_lib (dynamically updated)

min_price_to_sell = 15






nifty_pe_last_value = 0
nifty_ce_last_value = 0

# Token to subscribe once WebSocket is connected (set in main body, used in on_connect_solo)
nifty_instrument_token = None

start_mins_per_order_gap = 0
current_mins_per_order_gap = 0
last_mins_per_order_gap = 0
max_mins_left = 0

waiting_counter_secs = 0
nifty_control_log_counter = 0


def log_nifty_status(symbol_initials: str, nifty_pe_last_value: float, nifty_ce_last_value: float, nifty_current_val: float, ce_gap: float) -> None:
    """Log the Nifty status if the counter is a multiple of 10.

    Args:
        symbol_initials: The initial part of the Nifty symbol (e.g., NIFTY23DEC).
        nifty_pe_last_value: The last tracked PE strike value.
        nifty_ce_last_value: The last tracked CE strike value.
        nifty_current_val: The current Nifty spot price.
        ce_gap: The CE gap value for reference in the log.
    """
    global nifty_control_log_counter
    nifty_control_log_counter += 1
    if nifty_control_log_counter % 10 == 0:
        logging.error(
            f"{symbol_initials} Nifty still under control. Doing Nothing -- "
            f"Time = {get_ist_now().strftime('%H:%M:%S')} -- "
            f"pe_value = {nifty_pe_last_value} -- "
            f"CE value = {nifty_ce_last_value} "
            f"Current Nifty Value = {nifty_current_val} "
            f"Checking with CE gap of {ce_gap}"
        )


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

symbol_initials = ""   #This is to determine which week is considered eg: NIFTY23DEC


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
over_ride_flag = 0

quantity = ""
pe_quantity = 0
ce_quantity = 0

# Delta rebalancing state (initialised in main() from sys.argv)
enable_delta_rebalancing: bool = False
target_ce_delta: float = 0.0
target_pe_delta: float = 0.0
_NIFTY_STRIKE_SPACING: int = 50                                 # NIFTY: 50-pt option strikes
_NIFTY_RESET_LOG_THRESHOLD: float = _NIFTY_STRIKE_SPACING * 0.2  # 10 pts
# Minimum anchor shift required before a delta rebalance is applied (set in main())
_nifty_rebalance_min_shift: float = 5.0
# Anchor baseline for cumulative reset-gap logging (set in main(), reset on each sell/rebalance)
_pe_reset_last_logged_anchor: float = 0.0
_ce_reset_last_logged_anchor: float = 0.0


def return_pe_ce_gap(str):
    return_arr = {}
    return_arr['pe'] = str.split(":")[0]
    return_arr['ce'] = str.split(":")[1]
    return return_arr


def place_order_at_market_for_faroff_symbols(symbol, trans_type, exchange, order_type, quantity):

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
    #logging.info("Quote for Buy {}".format(quote_data))
    price = float(quote_data[exchange+":"+symbol]['last_price'])

    variety = kite.VARIETY_REGULAR 
    #variety = kite.VARIETY_AMO 


    if trans_type == kite.TRANSACTION_TYPE_BUY:
        price = price + 0.1
    else:
        price = price - 0.1


    print("Putting LIMIT order 10 paise off Market price "+ str(price))
    order_id = place_order(symbol, variety, trans_type, exchange, order_type, price, quantity)
    add_order_to_list(order_id, price, quantity, trans_type, symbol, "-1")
    return order_id


def place_order_at_market(symbol, trans_type, exchange, order_type, quantity):
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
    #logging.info("Quote for Buy {}".format(quote_data))
    price = float(quote_data[exchange+":"+symbol]['last_price']) 

    variety = kite.VARIETY_REGULAR 
    #variety = kite.VARIETY_AMO 

    order_id = place_order_market(symbol, variety, trans_type, exchange, order_type, quantity)
    add_order_to_list(order_id, price, quantity, trans_type, symbol, "-1")
    return order_id


import threading as _threading
import time as _time
import survivor_events_db as _events_db
import survivor_delta_rebalance as _delta_lib


def _rebalance_anchor_after_sell(sell_side: str, trigger_spot: float) -> None:
    """Background thread: fetch fresh positions 3 s after a sell, update anchors.

    Args:
        sell_side: "PE" or "CE" — which side was just sold.
        trigger_spot: Live spot price at the time of the sell.
    """
    global nifty_ce_last_value, nifty_pe_last_value, target_ce_delta, target_pe_delta
    _time.sleep(3)
    try:
        invalidate_positions_cache()
        fresh_positions = get_cached_positions().get("net", [])

        d_spot = _delta_lib.compute_expiry_delta_at_spot(
            fresh_positions, symbol_initials, trigger_spot, todays_volatility, interest_rate
        )
        d_ce = _delta_lib.compute_expiry_delta_at_spot(
            fresh_positions, symbol_initials, nifty_ce_last_value, todays_volatility, interest_rate
        )
        d_pe = _delta_lib.compute_expiry_delta_at_spot(
            fresh_positions, symbol_initials, nifty_pe_last_value, todays_volatility, interest_rate
        )

        if sell_side == "PE":
            computed_new_ce = _delta_lib.find_spot_for_target_delta(
                fresh_positions, symbol_initials, target_ce_delta,
                trigger_spot, todays_volatility, interest_rate,
                search_half_width=3000.0,
            )
            if computed_new_ce is not None:
                capped = min(computed_new_ce, trigger_spot - ce_gap)
                old_ce = nifty_ce_last_value
                if abs(capped - trigger_spot) < abs(old_ce - trigger_spot):
                    shift = abs(capped - old_ce)
                    if shift < _nifty_rebalance_min_shift:
                        logging.error(
                            f"Delta rebalance skipped: CE shift {shift:.1f} pts < "
                            f"min {_nifty_rebalance_min_shift:.1f} pts"
                        )
                    else:
                        nifty_ce_last_value = capped
                        logging.error(
                            f"Delta rebalance (PE sell): CE anchor {old_ce:.0f} → "
                            f"{nifty_ce_last_value:.0f}  spot={trigger_spot:.0f}"
                        )
                        _events_db.log_event(
                            os.getpid(), symbol_initials, "NIFTY", "CE_ANCHOR_REBALANCE",
                            spot_price=trigger_spot,
                            anchor_side="CE", old_anchor=old_ce, new_anchor=nifty_ce_last_value,
                            ce_anchor=nifty_ce_last_value, pe_anchor=nifty_pe_last_value,
                            delta_at_spot=d_spot, delta_at_ce_anchor=d_ce, delta_at_pe_anchor=d_pe,
                            notes=json.dumps({
                                "computed_raw": round(computed_new_ce, 2),
                                "capped": capped != computed_new_ce,
                            }),
                        )
        else:  # "CE"
            computed_new_pe = _delta_lib.find_spot_for_target_delta(
                fresh_positions, symbol_initials, target_pe_delta,
                trigger_spot, todays_volatility, interest_rate,
                search_half_width=3000.0,
            )
            if computed_new_pe is not None:
                capped = max(computed_new_pe, trigger_spot + pe_gap)
                old_pe = nifty_pe_last_value
                if abs(capped - trigger_spot) < abs(old_pe - trigger_spot):
                    shift = abs(capped - old_pe)
                    if shift < _nifty_rebalance_min_shift:
                        logging.error(
                            f"Delta rebalance skipped: PE shift {shift:.1f} pts < "
                            f"min {_nifty_rebalance_min_shift:.1f} pts"
                        )
                    else:
                        nifty_pe_last_value = capped
                        logging.error(
                            f"Delta rebalance (CE sell): PE anchor {old_pe:.0f} → "
                            f"{nifty_pe_last_value:.0f}  spot={trigger_spot:.0f}"
                        )
                        _events_db.log_event(
                            os.getpid(), symbol_initials, "NIFTY", "PE_ANCHOR_REBALANCE",
                            spot_price=trigger_spot,
                            anchor_side="PE", old_anchor=old_pe, new_anchor=nifty_pe_last_value,
                            ce_anchor=nifty_ce_last_value, pe_anchor=nifty_pe_last_value,
                            delta_at_spot=d_spot, delta_at_ce_anchor=d_ce, delta_at_pe_anchor=d_pe,
                            notes=json.dumps({
                                "computed_raw": round(computed_new_pe, 2),
                                "capped": capped != computed_new_pe,
                            }),
                        )

        # Refresh both targets from updated positions + updated anchors
        target_ce_delta = _delta_lib.compute_expiry_delta_at_spot(
            fresh_positions, symbol_initials, nifty_ce_last_value, todays_volatility, interest_rate
        )
        target_pe_delta = _delta_lib.compute_expiry_delta_at_spot(
            fresh_positions, symbol_initials, nifty_pe_last_value, todays_volatility, interest_rate
        )
        # Reset cumulative reset-gap baselines so future drift is measured from updated anchors
        global _pe_reset_last_logged_anchor, _ce_reset_last_logged_anchor
        _pe_reset_last_logged_anchor = nifty_pe_last_value
        _ce_reset_last_logged_anchor = nifty_ce_last_value
        _write_survivor_state(nifty_pe_last_value, nifty_ce_last_value)
        logging.error(
            f"Delta targets refreshed — CE: {target_ce_delta:.2f} @ {nifty_ce_last_value:.0f}, "
            f"PE: {target_pe_delta:.2f} @ {nifty_pe_last_value:.0f}"
        )

    except Exception as exc:
        logging.error(f"Delta rebalance failed after {sell_side} sell: {exc}", exc_info=True)


def check_times_to_sell_under_control(times_to_sell):
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
    # Callback to receive ticks.
    update_last_tick_time()
    global nifty_pe_last_value
    global nifty_ce_last_value

    global trans_type
    global exchange
    global order_type

    global nifty_lot_size
    global min_price_to_sell


    global pe_reset_gap_flag
    global ce_reset_gap_flag

    global pe_reset_gap
    global ce_reset_gap

    #print(ticks)
    #logging.error("New Ticks: {}".format(json.dumps(ticks, sort_keys=True, indent=4, default=str)))

    if nifty_pe_last_value == 0 or nifty_ce_last_value == 0:
        return


    nifty_current_val = ticks[0]['last_price']
    write_spot_price("NIFTY", nifty_current_val)
    if nifty_current_val > nifty_pe_last_value:
        if (nifty_current_val - nifty_pe_last_value) > pe_gap: 
            times_to_sell = int((nifty_current_val - nifty_pe_last_value)/pe_gap)
            if check_times_to_sell_under_control(times_to_sell):
                old_pe_anchor = nifty_pe_last_value
                nifty_pe_last_value = nifty_pe_last_value + pe_gap*times_to_sell
                _write_survivor_state(nifty_pe_last_value, nifty_ce_last_value)
                set_trigger_spot_price(nifty_current_val, "NIFTY")
                quantity_to_sell = times_to_sell*pe_quantity

                temp_pe_symbol_gap = pe_symbol_gap
                while True:
                    return_instrument_details = find_nifty_symbol_from_gap(symbol_initials, temp_pe_symbol_gap, "PE")
                    if return_instrument_details is None:
                        logging.error("Could not find PE symbol, skipping this tick")
                        break
                    logging.error("Symbol to Sell {}".format(return_instrument_details));
                    logging.error("Execute PE sell order Far off "+str(temp_pe_symbol_gap)+" Symbol = "+return_instrument_details['tradingsymbol']+" Quantity = "+str(quantity_to_sell)+" at market price pe_last_value = "+str(nifty_pe_last_value));
                    quote_data = kite.quote(exchange+":"+return_instrument_details['tradingsymbol'])
                    #logging.info("Quote for Buy {}".format(quote_data))
                    price = float(quote_data[exchange+":"+return_instrument_details['tradingsymbol']]['last_price']) 
                    if price < min_price_to_sell:
                        temp_pe_symbol_gap = temp_pe_symbol_gap - nifty_strike_gap
                    else:
                        break

                if return_instrument_details is None:
                    logging.error("Symbol not found, skipping order placement")
                else:
                    place_order_at_market(return_instrument_details['tradingsymbol'], trans_type, exchange, order_type, pe_quantity)
                    pe_reset_gap_flag = 1
                    _pe_reset_last_logged_anchor = nifty_pe_last_value  # reset cumulative baseline
                    _events_db.log_event(
                        os.getpid(), symbol_initials, "NIFTY", "ORDER_PLACED",
                        spot_price=nifty_current_val,
                        order_symbol=return_instrument_details['tradingsymbol'],
                        order_side="PE", order_quantity=pe_quantity,
                        ce_anchor=nifty_ce_last_value, pe_anchor=nifty_pe_last_value,
                    )
                    _events_db.log_event(
                        os.getpid(), symbol_initials, "NIFTY", "PE_ANCHOR_SHIFT",
                        spot_price=nifty_current_val,
                        anchor_side="PE", old_anchor=old_pe_anchor, new_anchor=nifty_pe_last_value,
                        ce_anchor=nifty_ce_last_value, pe_anchor=nifty_pe_last_value,
                    )
                    if enable_delta_rebalancing:
                        _threading.Thread(
                            target=_rebalance_anchor_after_sell,
                            args=("PE", nifty_current_val),
                            daemon=True, name="delta_rebalance_PE",
                        ).start()
            else:
                logging.error("Instructed not to continue ahead with PE trades")
        else:
            log_nifty_status(symbol_initials, nifty_pe_last_value, nifty_ce_last_value, nifty_current_val, ce_gap)


    if nifty_current_val < nifty_ce_last_value:
        if (nifty_ce_last_value - nifty_current_val) > ce_gap:
            #print("CE Gap = "+str(ce_gap))
            times_to_sell = int((nifty_ce_last_value - nifty_current_val)/ce_gap)
            if check_times_to_sell_under_control(times_to_sell):
                #print("Times to Sell  = "+str(times_to_sell))
                old_ce_anchor = nifty_ce_last_value
                nifty_ce_last_value = nifty_ce_last_value - ce_gap*times_to_sell
                _write_survivor_state(nifty_pe_last_value, nifty_ce_last_value)
                set_trigger_spot_price(nifty_current_val, "NIFTY")
                quantity_to_sell = times_to_sell*ce_quantity


                temp_ce_symbol_gap = ce_symbol_gap

                while True:
                    return_instrument_details = find_nifty_symbol_from_gap(symbol_initials, temp_ce_symbol_gap, "CE")
                    if return_instrument_details is None:
                        logging.error("Could not find CE symbol, skipping this tick")
                        break
                    #print("Quantity to Sell  = "+str(quantity_to_sell))
                    logging.error("Execute CE sell order Far off "+str(temp_ce_symbol_gap)+" Symbol = "+return_instrument_details['tradingsymbol']+" Quantity = "+str(quantity_to_sell)+" at market price ce_last_value = "+str(nifty_ce_last_value));
                    quote_data = kite.quote(exchange+":"+return_instrument_details['tradingsymbol']) 
                    price = float(quote_data[exchange+":"+return_instrument_details['tradingsymbol']]['last_price'])
                    if price < min_price_to_sell:
                        temp_ce_symbol_gap = temp_ce_symbol_gap - nifty_strike_gap
                    else:
                        break
                    
                if return_instrument_details is None:
                    logging.error("Symbol not found, skipping order placement")
                else:
                    place_order_at_market(return_instrument_details['tradingsymbol'], trans_type, exchange, order_type, ce_quantity)
                    ce_reset_gap_flag = 1
                    _ce_reset_last_logged_anchor = nifty_ce_last_value  # reset cumulative baseline
                    _events_db.log_event(
                        os.getpid(), symbol_initials, "NIFTY", "ORDER_PLACED",
                        spot_price=nifty_current_val,
                        order_symbol=return_instrument_details['tradingsymbol'],
                        order_side="CE", order_quantity=ce_quantity,
                        ce_anchor=nifty_ce_last_value, pe_anchor=nifty_pe_last_value,
                    )
                    _events_db.log_event(
                        os.getpid(), symbol_initials, "NIFTY", "CE_ANCHOR_SHIFT",
                        spot_price=nifty_current_val,
                        anchor_side="CE", old_anchor=old_ce_anchor, new_anchor=nifty_ce_last_value,
                        ce_anchor=nifty_ce_last_value, pe_anchor=nifty_pe_last_value,
                    )
                    if enable_delta_rebalancing:
                        _threading.Thread(
                            target=_rebalance_anchor_after_sell,
                            args=("CE", nifty_current_val),
                            daemon=True, name="delta_rebalance_CE",
                        ).start()
            else:
                logging.error("Instructed not to continue ahead with CE trades")

        else:
            log_nifty_status(symbol_initials, nifty_pe_last_value, nifty_ce_last_value, nifty_current_val, ce_gap)
        
    log_nifty_status(symbol_initials, nifty_pe_last_value, nifty_ce_last_value, nifty_current_val, ce_gap)

    #This code is to reset the pe and ce current values
    if (nifty_pe_last_value - nifty_current_val) > pe_reset_gap and pe_reset_gap_flag == 1:
        logging.error("Old PE value is "+str(nifty_pe_last_value));
        nifty_pe_last_value = nifty_current_val + pe_reset_gap
        _write_survivor_state(nifty_pe_last_value, nifty_ce_last_value)
        logging.error("Setting PE value as "+str(nifty_pe_last_value));
        if abs(_pe_reset_last_logged_anchor - nifty_pe_last_value) > _NIFTY_RESET_LOG_THRESHOLD:
            _events_db.log_event(
                os.getpid(), symbol_initials, "NIFTY", "RESET_GAP_TIGHTEN",
                spot_price=nifty_current_val,
                anchor_side="PE", old_anchor=_pe_reset_last_logged_anchor, new_anchor=nifty_pe_last_value,
                ce_anchor=nifty_ce_last_value, pe_anchor=nifty_pe_last_value,
            )
            _pe_reset_last_logged_anchor = nifty_pe_last_value

    if (nifty_current_val - nifty_ce_last_value) > ce_reset_gap and ce_reset_gap_flag == 1:
        logging.error("Old CE value is "+str(nifty_ce_last_value));
        nifty_ce_last_value = nifty_current_val - ce_reset_gap
        _write_survivor_state(nifty_pe_last_value, nifty_ce_last_value)
        logging.error("Setting CE value as "+str(nifty_ce_last_value));
        if abs(_ce_reset_last_logged_anchor - nifty_ce_last_value) > _NIFTY_RESET_LOG_THRESHOLD:
            _events_db.log_event(
                os.getpid(), symbol_initials, "NIFTY", "RESET_GAP_TIGHTEN",
                spot_price=nifty_current_val,
                anchor_side="CE", old_anchor=_ce_reset_last_logged_anchor, new_anchor=nifty_ce_last_value,
                ce_anchor=nifty_ce_last_value, pe_anchor=nifty_pe_last_value,
            )
            _ce_reset_last_logged_anchor = nifty_ce_last_value


def on_connect_solo(ws, response):  # noqa
    """Callback on successful WebSocket connect.

    Subscribes to the nifty instrument token here (not in the main thread)
    to avoid calling sendMessage before the connection is open.
    Also checks on any pending orders on reconnect (mirrors common_lib behavior).
    """
    global nifty_instrument_token
    logging.error("WebSocket connected (on_connect_solo)")

    # Check pending orders on reconnect (same as common_lib.on_connect_solo)
    orders = get_orders()
    logging.info("Current Solo Order Conditions {}".format(orders))
    if len(orders) > 0:
        check_orders_solo()

    # Subscribe to the instrument token — must happen inside on_connect, not in
    # the main thread, because kws.ws is None until the handshake completes.
    if nifty_instrument_token is not None:
        subscribe_for_tick([nifty_instrument_token])
        logging.error(
            "on_connect_solo: subscribed to token %d — ticks flowing",
            nifty_instrument_token,
        )
    else:
        # Race condition: WebSocket connected before main thread finished setting the token.
        # main() will call subscribe_for_tick() unconditionally after setting the token —
        # that call uses common_lib.kws (live instance) and will succeed now that we're connected.
        logging.warning(
            "on_connect_solo: nifty_instrument_token not set yet (WebSocket connected faster "
            "than quote API). main() will subscribe directly after setting the token."
        )


def on_order_update_solo_callback(order_id, order_update_status): #order_update_status can be of 3 types complete, cancelled, other

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


def main() -> None:
    """Main execution function for the script."""
    global symbol_initials, pe_symbol_gap, ce_symbol_gap, exchange, order_type
    global pe_gap, ce_gap, pe_reset_gap, ce_reset_gap, pe_quantity, ce_quantity
    global pe_start_point, ce_start_point, request_token, over_ride_flag
    global current_number, pe_current_gap, ce_current_gap, trans_type
    global start_number, temp_trans_type, nifty_pe_last_value, nifty_ce_last_value, nifty_instrument_token
    global enable_delta_rebalancing, target_ce_delta, target_pe_delta

    try:
        symbol_initials = sys.argv[1]

        print(len(symbol_initials))
        if len(symbol_initials) < 9:
            print("\n\n\n\n\nThe symbol is not complete. Please entre correct Symbol \n\n\n\n\n")
            quit()

        symbol_gap = return_pe_ce_gap(sys.argv[2])
        pe_symbol_gap = int(symbol_gap['pe'])
        ce_symbol_gap = int(symbol_gap['ce'])

        exchange_temp = sys.argv[3]
        exchange = exchange_temp
        if ":" in exchange_temp:
            exchange = exchange_temp.split(":")[0]
            order_type = exchange_temp.split(":")[1]
            if order_type != "MIS":
                print("Only Order Type Supported is MIS. Quitting....")
                quit()

        gap = return_pe_ce_gap(sys.argv[4])
        pe_gap = float(gap['pe'])
        ce_gap = float(gap['ce'])

        reset_gap = return_pe_ce_gap(sys.argv[5])
        pe_reset_gap = float(reset_gap['pe'])
        ce_reset_gap = float(reset_gap['ce'])

        quantity = return_pe_ce_gap(sys.argv[6])
        pe_quantity = int(quantity['pe'])
        ce_quantity = int(quantity['ce'])

        start_point = return_pe_ce_gap(sys.argv[7])
        pe_start_point = int(float(start_point['pe']))
        ce_start_point = int(float(start_point['ce']))

        request_token = sys.argv[8]
        if len(sys.argv) > 9:
            argv_9 = sys.argv[9].strip()
            if argv_9 == "True":
                over_ride_flag = 1
            elif argv_9 == "delta_rebalancing":
                enable_delta_rebalancing = True

        global _nifty_rebalance_min_shift
        _nifty_rebalance_min_shift = min(_NIFTY_STRIKE_SPACING * 0.1, pe_gap, ce_gap)
        logging.error(
            f"Rebalance min shift: {_nifty_rebalance_min_shift:.1f} pts "
            f"(min({_NIFTY_STRIKE_SPACING * 0.1}, {pe_gap}, {ce_gap}))"
        )
    except Exception as e:
        logging.error("%s", e)
        logging.error(
            "Format -- python3  place_order_at_nifty.py <SYMBOL_INITIALS> "
            "<PE_SYMBOL_GAP>:<CE_SYMBOL_GAP> <EXCHANGE> <PE GAP To Sell After>:<CE GAP To Sell After> "
            "<PE Reset Gap>:<CE Reset Gap> <PE QUANTITY>:<CE QUANTITY> "
            "<PE_START_POINT>:<CE_START_POINT> <REQUET_TOKEN>"
        )
        logging.error(
            "Sample-- python3 place_order_at_nifty.py NIFTY23DEC 200:250 NFO 20:30 30:50 300:250 0:0 aa"
        )
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

    if order_type == "":
        if exchange == "NFO":
            order_type = "NRML"
        if exchange == "NSE":
            order_type = "CNC"

    initilise_basic(request_token, on_ticks_update, on_connect_solo, on_order_update_solo)
    set_tag("Trending_Market_Code")

    set_on_order_update_solo_callback(on_order_update_solo_callback)

    nifty_value = get_nifty_current_quote()

    if pe_start_point == 0:
        nifty_pe_last_value = nifty_value['last_price']
    else:
        nifty_pe_last_value = pe_start_point

    if ce_start_point == 0:
        nifty_ce_last_value = nifty_value['last_price']
    else:
        nifty_ce_last_value = ce_start_point

    _write_survivor_state(nifty_pe_last_value, nifty_ce_last_value)
    logging.error("---------- Nifty PE Start Value  : {}".format(nifty_pe_last_value))
    logging.error("---------- Nifty CE Start Value  : {}".format(nifty_ce_last_value))

    # --- Delta rebalancing init ---
    _events_db.init_db()
    _init_positions = get_cached_positions().get("net", [])
    if enable_delta_rebalancing:
        target_ce_delta = _delta_lib.compute_expiry_delta_at_spot(
            _init_positions, symbol_initials, nifty_ce_last_value, todays_volatility, interest_rate
        )
        target_pe_delta = _delta_lib.compute_expiry_delta_at_spot(
            _init_positions, symbol_initials, nifty_pe_last_value, todays_volatility, interest_rate
        )
    # Initialise cumulative reset-gap baselines from starting anchors
    global _pe_reset_last_logged_anchor, _ce_reset_last_logged_anchor
    _pe_reset_last_logged_anchor = nifty_pe_last_value
    _ce_reset_last_logged_anchor = nifty_ce_last_value
    _events_db.log_event(
        os.getpid(), symbol_initials, "NIFTY", "INIT",
        spot_price=nifty_value['last_price'],
        ce_anchor=nifty_ce_last_value, pe_anchor=nifty_pe_last_value,
        delta_at_ce_anchor=target_ce_delta if enable_delta_rebalancing else None,
        delta_at_pe_anchor=target_pe_delta if enable_delta_rebalancing else None,
        notes=json.dumps({
            "rebalancing": enable_delta_rebalancing,
            "target_ce_delta": round(target_ce_delta, 2),
            "target_pe_delta": round(target_pe_delta, 2),
            "volatility": todays_volatility,
            "interest_rate": interest_rate,
        }),
    )
    logging.error(
        f"Survivor init: rebalancing={enable_delta_rebalancing}, "
        f"ce_anchor={nifty_ce_last_value}, pe_anchor={nifty_pe_last_value}, "
        f"target_ce_delta={target_ce_delta:.2f}, target_pe_delta={target_pe_delta:.2f}"
    )

    # Store token so on_connect_solo can subscribe once the WebSocket is open.
    nifty_instrument_token = nifty_value['instrument_token']
    logging.error("Instrument token stored for subscription: {}".format(nifty_instrument_token))

    # Race-condition fix: 'from common_lib import *' binds the local name 'kws' to
    # None at import time. When initilise_basic() later sets common_lib.kws = KiteTicker(),
    # the local 'kws' never updates — so checking 'kws is not None' is always False.
    #
    # Fix: call subscribe_for_tick() unconditionally. subscribe_for_tick() accesses
    # common_lib.kws directly (via its own 'global kws'), so it gets the live KiteTicker.
    # • If WebSocket is already connected (race condition path): subscribes now, ticks flow.
    # • If WebSocket not yet connected: raises (kws.ws is None), we catch it, and
    #   on_connect_solo will subscribe via nifty_instrument_token when it fires.
    # In both cases _subscribed_tokens is stored first inside subscribe_for_tick()
    # so reconnect recovery also has the token.
    try:
        subscribe_for_tick([nifty_instrument_token])
        logging.error(
            "WebSocket subscription successful (direct path) for token: %d — "
            "ticks will start arriving",
            nifty_instrument_token,
        )
    except Exception as subscribe_error:
        logging.info(
            "WebSocket not yet connected (token %d stored); on_connect_solo will "
            "subscribe when connection completes. (error: %s)",
            nifty_instrument_token,
            subscribe_error,
        )

    get_all_fut_opt_instruments()

    # subscription happens inside on_connect_solo callback (or fallback above)
    logging.info("Waiting for WebSocket connection to subscribe...")

    watchdog_sleep()

    logging.error("single leg gtt order trigger_id : {}".format(nifty_value))
    quit()


if __name__ == "__main__":
    main()
