"""funpop_core.py — Data prep + HTML template, extracted from the original dashboard script.
Loading from CSV, file picker, GitHub push, and main() are removed — those live in main.py.
Everything else (constants, classify, build_rankings, build_core, build_dc_analysis,
prepare_data, HTML template, _sanitize, build_html) is unchanged."""

import json, base64, os, math
from pathlib import Path
from itertools import combinations as _combos
import pandas as pd
import numpy as np

ACTIVE_ITEMS = [658442130, 666209064, 658442128]
BIN_ITEMS    = [658442130, 658442128]
SHELF_ITEMS  = [666209064]

CONTAINER = {
    658442128: {'units': 208, 'label': 'bins'},  # Full Bin
    658442130: {'units': 126, 'label': 'bins'},  # Half Bin
    666209064: {'units': 6,   'label': 'cases'},
}

REQUIRED = ['business_date','walmart_calendar_week','store_number','item_name',
            'walmart_item_number','state_or_province_code',
            'pos_quantity_this_year','pos_quantity_last_year',
            'store_on_hand_quantity_this_year','store_on_hand_quantity_last_year',
            'store_in_warehouse_quantity_this_year','store_in_transit_quantity_this_year',
            'store_specific_retail_amount_this_year','pos_sales_this_year','pos_sales_last_year']

# DC INVENTORY FILE — schema for the DC daily report
DC_REQUIRED = ['inventory_date','distribution_center_number','name_of_the_dc',
               'walmart_item_number','item_name',
               'on_hand_warehouse_inventory_in_units_this_year',
               'on_hand_warehouse_inventory_in_units_last_year',
               'on_order_warehouse_quantity_in_units_this_year',
               'on_order_warehouse_quantity_in_units_last_year',
               'out_of_stock_each_quantity_this_year',
               'out_of_stock_each_quantity_last_year']

# ── Classification ────────────────────────────────────────────────────────────
def classify(ty, ly):
    if ty == 0:  return 'Zero'
    if ty > ly:  return 'Up'
    if ty < ly:  return 'Down'
    return 'Flat'

# ── Store rankings ────────────────────────────────────────────────────────────
def build_rankings(df_in, _shared=None):
    """_shared: dict of pre-built lookups from prepare_data (on_hand, vel, cw_status).
    When provided, skips expensive recomputation for each combo call."""
    grp_cols = ['store_number', 'state_or_province_code']
    if 'store_name' in df_in.columns: grp_cols.append('store_name')
    if 'city_name'  in df_in.columns: grp_cols.append('city_name')

    # ── Full-period aggregation (vectorized, no itertuples) ───────────────────
    st = df_in.groupby(grp_cols, sort=False).agg(
        ty=('pos_quantity_this_year','sum'),
        ly=('pos_quantity_last_year','sum'),
        sales_ty=('pos_sales_this_year','sum'),
        sales_ly=('pos_sales_last_year','sum'),
        days=('business_date','nunique'),
    ).reset_index()
    _ly_safe = st['ly'].replace(0, float('nan'))
    _sly_safe = st['sales_ly'].replace(0, float('nan'))
    st['yoy']       = (st['ty'] - st['ly']).astype(int)
    st['yoy_pct']   = ((st['yoy'] / _ly_safe) * 100).round(1).fillna(0)
    st['yoy_sales'] = (st['sales_ty'] - st['sales_ly']).round(2)
    st['yoy_sales_pct'] = ((st['yoy_sales'] / _sly_safe) * 100).round(1).fillna(0)
    st['uspd']      = (st['ty'] / st['days'].replace(0, float('nan'))).round(2).fillna(0)
    st['rank_ty']   = st['ty'].rank(method='min', ascending=False).astype(int)
    st['rank_ly']   = st['ly'].rank(method='min', ascending=False).astype(int)
    st['rank_chg']  = (st['rank_ly'] - st['rank_ty']).astype(int)
    st = st.sort_values('rank_ty')

    # ── Use shared lookups if provided, else build them (first/all-items call) ─
    if _shared:
        _oh_df    = _shared['oh_df']
        _vel_df   = _shared['vel_df']
        _cw_df    = _shared['cw_df']
    else:
        _max_d = df_in['business_date'].max()
        _recent = df_in[df_in['business_date'] == _max_d]
        _oh_df = _recent.groupby('store_number', sort=False).agg(
            on_hand=('store_on_hand_quantity_this_year','sum'),
            on_hand_ly=('store_on_hand_quantity_last_year','sum'),
            cur_price=('store_specific_retail_amount_this_year','max'),
        ).reset_index()
        _wks   = sorted(df_in['walmart_calendar_week'].unique())
        _vel_df = df_in[df_in['walmart_calendar_week'].isin(_wks[-4:])].groupby(
            ['store_number','walmart_calendar_week'], sort=False
        ).agg(ty_w=('pos_quantity_this_year','sum'), ly_w=('pos_quantity_last_year','sum')).reset_index()
        _cw_df = df_in[df_in['walmart_calendar_week'] == _wks[-1]].groupby(
            'store_number', sort=False
        ).agg(ty_cw=('pos_quantity_this_year','sum'), ly_cw=('pos_quantity_last_year','sum')).reset_index()

    # ── Merge all lookups at once (vectorized, no per-row dicts) ─────────────
    st = st.merge(_oh_df[['store_number','on_hand','on_hand_ly','cur_price']], on='store_number', how='left')
    st = st.merge(_cw_df[['store_number','ty_cw','ly_cw']], on='store_number', how='left')
    for col, fill in [('on_hand',0),('on_hand_ly',0),('cur_price',0.0),('ty_cw',0),('ly_cw',0)]:
        st[col] = st[col].fillna(fill)
    import numpy as _np3
    st['status'] = _np3.where(st['ty_cw']==0,'Zero',
        _np3.where(st['ty_cw']>st['ly_cw'],'Up',
        _np3.where(st['ty_cw']<st['ly_cw'],'Down','Flat')))

    # ── Build trend map from vel_df (pivot is faster than groupby-iterate) ────
    if len(_vel_df):
        _vpivot = _vel_df.pivot_table(index='store_number', columns='walmart_calendar_week',
            values=['ty_w','ly_w'], aggfunc='sum', fill_value=0)
        _vpivot.columns = [f'{c[0]}_{c[1]}' for c in _vpivot.columns]
        _wk_cols = sorted([c for c in _vel_df['walmart_calendar_week'].unique()])
        _vel_records = {}
        for sn, row in _vpivot.iterrows():
            _vel_records[int(sn)] = [
                {'ty': int(row.get(f'ty_w_{w}',0)), 'ly': int(row.get(f'ly_w_{w}',0)),
                 'dir': ('Up' if row.get(f'ty_w_{w}',0) > row.get(f'ly_w_{w}',0)
                         else ('Down' if row.get(f'ty_w_{w}',0) < row.get(f'ly_w_{w}',0)
                               else ('Zero' if row.get(f'ty_w_{w}',0)==0 else 'Flat')))}
                for w in reversed(_wk_cols)
            ]
    else:
        _vel_records = {}

    # ── Convert to records (to_dict is faster than itertuples for wide dfs) ───
    _has_name = 'store_name' in grp_cols
    _has_city = 'city_name'  in grp_cols
    rows = []
    for rec in st.to_dict('records'):
        sn = int(rec['store_number'])
        _nm = rec.get('store_name') if _has_name else None
        _cy = rec.get('city_name')  if _has_city else None
        rows.append({
            'rank_ty':  int(rec['rank_ty']),
            'rank_ly':  int(rec['rank_ly']),
            'rank_chg': int(rec['rank_chg']),
            'store':    sn,
            'name':     str(_nm) if _nm is not None and _nm == _nm else f'Store {sn}',
            'city':     str(_cy) if _cy is not None and _cy == _cy else '',
            'state':    str(rec['state_or_province_code']),
            'ty':       int(rec['ty']), 'ly': int(rec['ly']),
            'yoy':      int(rec['yoy']), 'yoy_pct': float(rec['yoy_pct']),
            'sales_ty': round(float(rec['sales_ty']), 2),
            'yoy_sales':round(float(rec['yoy_sales']), 2),
            'yoy_sales_pct': float(rec['yoy_sales_pct']),
            'uspd':     float(rec['uspd']),
            'on_hand':  int(rec['on_hand']), 'on_hand_ly': int(rec['on_hand_ly']),
            'cur_price':round(float(rec['cur_price']), 2),
            'status':   str(rec['status']),
            'trend':    _vel_records.get(sn, []),
        })
    return rows

def _fmt_date(ts):
    """Cross-platform short date label — no leading zeros, works on Windows + Linux."""
    return f"{ts.month}/{ts.day}"

def _iso(d):
    """Convert any date type to ISO YYYY-MM-DD string (safe for numpy datetime64)."""
    try: return pd.Timestamp(d).strftime('%Y-%m-%d')
    except Exception: return str(d)[:10]

# ── Core data builder ─────────────────────────────────────────────────────────

def build_core(df_in, weeks, _shared=None):
    if df_in.empty:
        print(' WARNING: build_core received empty dataframe — check item numbers match ACTIVE_ITEMS')
        return {}
    print(f' build_core: {len(df_in):,} rows, {df_in["store_number"].nunique()} stores, {len(weeks)} weeks')

    # Weekly store counts (deduped at store level first)
    weekly = df_in.groupby(['store_number','walmart_calendar_week']).agg(
        ty=('pos_quantity_this_year','sum'), ly=('pos_quantity_last_year','sum')
    ).reset_index()
    # Vectorized status (much faster than apply with axis=1)
    import numpy as _np
    _ty_arr, _ly_arr = weekly['ty'].values, weekly['ly'].values
    weekly['status'] = _np.where(_ty_arr == 0, 'Zero',
                       _np.where(_ty_arr > _ly_arr, 'Up',
                       _np.where(_ty_arr < _ly_arr, 'Down', 'Flat')))

    summary_data = []
    for wk in weeks:
        w = weekly[weekly['walmart_calendar_week']==wk]
        summary_data.append({'week':f"WK {str(int(wk))[-2:]}",
            'Up':int((w['status']=='Up').sum()),'Down':int((w['status']=='Down').sum()),
            'Zero':int((w['status']=='Zero').sum()),'Flat':int((w['status']=='Flat').sum()),
            'Total':int(len(w))})

    # Current week composition
    curr_wk = int(df_in['walmart_calendar_week'].max())
    curr = weekly[weekly['walmart_calendar_week']==curr_wk]
    ct = max(len(curr), 1)
    curr_comp = {'week':f"WK {str(curr_wk)[-2:]}",'total':int(len(curr)),
        'Up':int((curr['status']=='Up').sum()),'Down':int((curr['status']=='Down').sum()),
        'Zero':int((curr['status']=='Zero').sum()),'Flat':int((curr['status']=='Flat').sum())}
    for k in ['Up','Down','Zero','Flat']:
        curr_comp[k+'_pct'] = round(curr_comp[k]/ct*100, 1)

    # True U/S/W per week
    uspw_rows = []
    for wk in weeks:
        sw = df_in[df_in['walmart_calendar_week']==wk].groupby('store_number').agg(
            ty=('pos_quantity_this_year','sum'), ly=('pos_quantity_last_year','sum')
        ).reset_index()
        total_s = max(len(sw), 1)
        sell_ty = int((sw['ty']>0).sum()); sell_ly = int((sw['ly']>0).sum())
        tot_ty = float(sw['ty'].sum()); tot_ly = float(sw['ly'].sum())
        uspw_rows.append({'week':f"WK {str(int(wk))[-2:]}",
            'ty_all':round(tot_ty/total_s,2),'ty_sell':round(tot_ty/sell_ty,2) if sell_ty else 0,
            'ly_all':round(tot_ly/total_s,2),'ly_sell':round(tot_ly/sell_ly,2) if sell_ly else 0,
            'sell_ty':sell_ty,'total_s':total_s})

    # U/S/W by price point — single grouped pass, then split by date (no per-date groupby)
    _pp_src = df_in[df_in['store_specific_retail_amount_this_year']>0].copy()
    _pp_dcol = '_business_date_only' if '_business_date_only' in _pp_src.columns else 'business_date'
    if _pp_dcol == 'business_date':
        _pp_src['_business_date_only'] = _pp_src['business_date'].dt.normalize()
        _pp_dcol = '_business_date_only'
    # One groupby across ALL dates at once — then split by date
    _pp_agg = _pp_src.groupby([_pp_dcol, 'store_specific_retail_amount_this_year'], sort=False).agg(
        stores=('store_number','nunique'), units_ty=('pos_quantity_this_year','sum'),
        units_ly=('pos_quantity_last_year','sum'), sales_ty=('pos_sales_this_year','sum'),
        sales_ly=('pos_sales_last_year','sum'),
    ).reset_index()
    all_pp_dates = sorted(_pp_agg[_pp_dcol].unique())
    _pp_by_date_raw = {}
    for _rec in _pp_agg.to_dict('records'):
        _dstr = _iso(_rec[_pp_dcol])
        _pp_by_date_raw.setdefault(_dstr, []).append(_rec)
    def _build_pp_rows(_recs):
        _out = []
        for _rec in _recs:
            _p=_rec['store_specific_retail_amount_this_year']; _s=max(_rec['stores'],1)
            _out.append({'price':float(_p),'label':f'${_p:.2f}','stores':int(_rec['stores']),
                'uspw_ty':round(_rec['units_ty']/_s*7,2),'uspw_ly':round(_rec['units_ly']/_s*7,2),
                'spw_ty':round(_rec['sales_ty']/_s*7,2),'spw_ly':round(_rec['sales_ly']/_s*7,2),
                'units_ty':int(_rec['units_ty']),'units_ly':int(_rec['units_ly']),
                'sales_ty':round(float(_rec['sales_ty']),2),'sales_ly':round(float(_rec['sales_ly']),2)})
        _out.sort(key=lambda x: x['stores'], reverse=True)
        return _out
    pp_by_date = {_d: _build_pp_rows(_recs) for _d, _recs in _pp_by_date_raw.items()}
    pp_date_list = [_iso(_d) for _d in all_pp_dates]
    latest_pp_date = pp_date_list[-1] if pp_date_list else ''
    pp_list = pp_by_date.get(latest_pp_date, [])

    # Daily
    daily = df_in.groupby('business_date').agg(
        ty=('pos_quantity_this_year','sum'),ly=('pos_quantity_last_year','sum')
    ).reset_index().sort_values('business_date')
    daily['yoy'] = daily['ty'] - daily['ly']
    daily['yoy_pct'] = ((daily['ty']-daily['ly'])/daily['ly'].replace(0,float('nan')))*100
    daily['date'] = daily['business_date'].dt.date

    ld = daily['business_date'].max()
    last4w = daily[daily['business_date']>=ld-pd.Timedelta(days=27)].reset_index(drop=True)
    last7d = daily[daily['business_date']>=ld-pd.Timedelta(days=6)].sort_values('business_date',ascending=False).reset_index(drop=True)
    chart4w = [{'date':str(r.date),'ty':int(r.ty),'ly':int(r.ly)} for r in last4w.itertuples()]
    tracker7d = [{'date':r.date.strftime('%b %d'),'day':r.date.strftime('%a'),
                  'ty':int(r.ty),'ly':int(r.ly),'yoy':int(r.yoy),
                  'yoy_pct':round(float(r.yoy_pct),1) if not pd.isna(r.yoy_pct) else 0}
                 for r in last7d.itertuples()]
    t7 = last7d.agg({'ty':'sum','ly':'sum','yoy':'sum'})
    t7_pct = round((t7['yoy']/t7['ly']*100) if t7['ly'] else 0, 1)

    # Period KPIs
    pty=int(df_in['pos_quantity_this_year'].sum()); ply=int(df_in['pos_quantity_last_year'].sum())
    pct=round(((pty-ply)/ply*100) if ply else 0, 1)
    dr=(f"{df_in['business_date'].min().strftime('%b %d')} \u2013 "
        f"{df_in['business_date'].max().strftime('%b %d, %Y')}")

    # ── Inventory uses 'recent' (most recent day) — compute once ──────────────
    _max_d = df_in['business_date'].max()
    recent = df_in[df_in['business_date'] == _max_d]

    # Inventory with bins/cases
    inv_grp = recent.groupby(['walmart_item_number','item_name']).agg(
        stores=('store_number','nunique'),
        on_hand_ty=('store_on_hand_quantity_this_year','sum'),
        on_hand_ly=('store_on_hand_quantity_last_year','sum'),
        warehouse=('store_in_warehouse_quantity_this_year','sum'),
        in_transit=('store_in_transit_quantity_this_year','sum'),
    ).reset_index()
    last4_wks = sorted(df_in['walmart_calendar_week'].unique())[-4:]
    vel_df = df_in[df_in['walmart_calendar_week'].isin(last4_wks)]
    wkly_vel = vel_df.groupby('walmart_calendar_week')['pos_quantity_this_year'].sum().mean()
    inv_list=[]
    for r in inv_grp.itertuples():
        cont=CONTAINER.get(int(r.walmart_item_number),{'units':1,'label':'units'})
        cu=cont['units']; cl=cont['label']
        oh=int(r.on_hand_ty); wh=int(r.warehouse); it=int(r.in_transit); tot=oh+wh+it
        wos=round(oh/wkly_vel,1) if wkly_vel else 0
        inv_list.append({'item':r.item_name,'stores':int(r.stores),
            'on_hand_ty':oh,'on_hand_ly':int(r.on_hand_ly),'warehouse':wh,'in_transit':it,
            'total_supply':tot,'wos':float(wos),'container_units':cu,'container_label':cl,
            'on_hand_cont':int(oh/cu),'warehouse_cont':int(wh/cu),
            'in_transit_cont':int(it/cu),'total_cont':int(tot/cu)})

    rankings = build_rankings(df_in, _shared=_shared)

    # ── Retail daily — per-combo so item filter affects store counts ──────────
    # Each store is counted at EXACTLY ONE price per day — the price where it sold
    # the most units. This handles Walmart's data correctly when the same store
    # legitimately had multiple prices on one day (mid-day price change) or stale
    # zero-unit rows at old prices. Without this dedup, summing across prices on
    # 5/11 produced > total store count (impossible).
    _rp_src = df_in[df_in['store_specific_retail_amount_this_year']>0].copy()
    _rp_src['bdate'] = _rp_src['business_date'].dt.normalize()
    # Aggregate units to (store, date, price) so each combination is one row
    _rp_sdp = _rp_src.groupby(['bdate','store_number','store_specific_retail_amount_this_year'], sort=False).agg(
        qty_ty=('pos_quantity_this_year','sum'),
        qty_ly=('pos_quantity_last_year','sum'),
    ).reset_index()
    # Keep only selling rows, then per (store, date) keep the dominant (max qty) price
    _rp_sdp = _rp_sdp[_rp_sdp['qty_ty']>0]
    _rp_sdp = _rp_sdp.sort_values('qty_ty', ascending=False).drop_duplicates(['bdate','store_number'], keep='first')
    # Now (date, price) groupby: each store counted exactly once
    _rp_c = _rp_sdp.groupby(['bdate','store_specific_retail_amount_this_year'], sort=False).agg(
        stores=('store_number','count'),
        units_ty=('qty_ty','sum'),
        units_ly=('qty_ly','sum'),
    ).reset_index().sort_values('bdate')
    _prices_c = sorted(_rp_c['store_specific_retail_amount_this_year'].unique())
    _dates_c   = sorted(_rp_c['bdate'].unique())
    # Pre-compute date strings/labels once (was being recomputed inside nested loop)
    _date_str_c   = {_dc: pd.Timestamp(_dc).strftime('%Y-%m-%d') for _dc in _dates_c}
    _date_label_c = {_dc: _fmt_date(pd.Timestamp(_dc)) for _dc in _dates_c}
    _rp_map_c  = {(_date_str_c[r.bdate], float(r.store_specific_retail_amount_this_year)):r
                  for r in _rp_c.itertuples()}
    _rd_combo = []
    for _pc in _prices_c:
        _drows = []
        for _dc in _dates_c:
            _dsc = _date_str_c[_dc]
            _rc = _rp_map_c.get((_dsc, float(_pc)))
            _sc = max(_rc.stores, 1) if _rc else 1
            _drows.append({'date':_dsc,'date_label':_date_label_c[_dc],
                'stores':int(_rc.stores) if _rc else 0,
                'units_ty':int(_rc.units_ty) if _rc else 0,
                'units_ly':int(_rc.units_ly) if _rc else 0,
                'uspw_ty':round(_rc.units_ty/_sc,2) if _rc else 0.0,
                'uspw_ly':round(_rc.units_ly/_sc,2) if _rc else 0.0,
                'yoy':(int(_rc.units_ty)-int(_rc.units_ly)) if _rc else 0})
        _currc=_drows[-1] if _drows else {}; _prevc=_drows[-2] if len(_drows)>=2 else {}
        _rd_combo.append({'price':float(_pc),'label':f'${_pc:.2f}',
            'curr_stores':_currc.get('stores',0),'prev_stores':_prevc.get('stores',0),
            'store_delta':_currc.get('stores',0)-_prevc.get('stores',0),
            'curr_uspw':_currc.get('uspw_ty',0.0),'days':_drows})
    _rd_combo.sort(key=lambda x: x['curr_stores'], reverse=True)
    _rd_combo = _rd_combo[:2]

    return {
        'summary':summary_data,'curr_comp':curr_comp,'uspw':uspw_rows,'uspw_pp':pp_list,'uspw_pp_by_date':pp_by_date,'pp_date_list':pp_date_list,'latest_pp_date':latest_pp_date,
        'chart4w':chart4w,'tracker7d':tracker7d,
        'kpi':{'ty':pty,'ly':ply,'yoy':pty-ply,'yoy_pct':pct,'date_range':dr},
        'totals7d':{'ty':int(t7['ty']),'ly':int(t7['ly']),'yoy':int(t7['yoy']),'pct':t7_pct},
        
        'inventory':inv_list,'inv_date':ld.strftime('%b %d, %Y'),
        'rankings':rankings,
        'retail_daily':_rd_combo,
    }


# ══════════════════════════════════════════════════════════════════════════════
# DC INVENTORY — optional add-on (cached so it persists between runs)

def build_dc_analysis(dc_df, sales_df):
    """
    Cross-reference DC inventory with store-level sales velocity.
    Returns the structured DC analysis bundle for the dashboard.
    """
    import numpy as _np_dc
    if dc_df is None or dc_df.empty:
        return None

    max_inv_d = dc_df['inventory_date'].max()
    recent = dc_df[dc_df['inventory_date']==max_inv_d].copy()

    # 7-day sales velocity per DC per item (only works if sales file has DC cols)
    has_dc_col = 'distribution_center_number' in sales_df.columns
    if has_dc_col:
        max_sales_d = sales_df['business_date'].max()
        l7 = sales_df[sales_df['business_date']>=max_sales_d-pd.Timedelta(days=6)]
        vel = l7.dropna(subset=['distribution_center_number']).groupby(
            ['distribution_center_number','walmart_item_number'], sort=False
        ).agg(weekly_sales=('pos_quantity_this_year','sum'),
              stores=('store_number','nunique')).reset_index()
        vel['daily_sales'] = vel['weekly_sales']/7
    else:
        vel = pd.DataFrame(columns=['distribution_center_number','walmart_item_number',
                                    'weekly_sales','stores','daily_sales'])

    combined = recent.merge(vel, on=['distribution_center_number','walmart_item_number'], how='left').fillna({
        'weekly_sales':0,'daily_sales':0,'stores':0})

    combined['days_oh'] = _np_dc.where(combined['daily_sales']>0,
        combined['on_hand_warehouse_inventory_in_units_this_year']/combined['daily_sales'].replace(0,_np_dc.nan),
        _np_dc.where(combined['on_hand_warehouse_inventory_in_units_this_year']>0, 999, 0))
    combined['total_supply'] = (combined['on_hand_warehouse_inventory_in_units_this_year']
                                + combined['on_order_warehouse_quantity_in_units_this_year'])
    combined['days_total'] = _np_dc.where(combined['daily_sales']>0,
        combined['total_supply']/combined['daily_sales'].replace(0,_np_dc.nan),
        _np_dc.where(combined['total_supply']>0, 999, 0))
    combined['wos_oh'] = (combined['days_oh']/7).round(1)
    combined['wos_total'] = (combined['days_total']/7).round(1)

    # System KPIs
    total_oh = float(combined['on_hand_warehouse_inventory_in_units_this_year'].sum())
    total_oo = float(combined['on_order_warehouse_quantity_in_units_this_year'].sum())
    total_oh_ly = float(combined['on_hand_warehouse_inventory_in_units_last_year'].sum())
    total_oo_ly = float(combined['on_order_warehouse_quantity_in_units_last_year'].sum())
    total_wk = float(combined['weekly_sales'].sum())
    total_oos_ty = float(dc_df['out_of_stock_each_quantity_this_year'].sum())
    total_oos_ly = float(dc_df['out_of_stock_each_quantity_last_year'].sum())
    kpi = {
        'on_hand': int(total_oh), 'on_order': int(total_oo),
        'on_hand_ly': int(total_oh_ly), 'on_order_ly': int(total_oo_ly),
        'on_hand_yoy_pct': round((total_oh-total_oh_ly)/total_oh_ly*100,1) if total_oh_ly>0 else 0,
        'on_order_yoy_pct': round((total_oo-total_oo_ly)/total_oo_ly*100,1) if total_oo_ly>0 else 0,
        'total_supply': int(total_oh+total_oo),
        'weekly_sales': int(total_wk),
        'wos_oh': round(total_oh/total_wk,1) if total_wk>0 else 0,
        'wos_total': round((total_oh+total_oo)/total_wk,1) if total_wk>0 else 0,
        'oos_ty': int(total_oos_ty),
        'oos_ly': int(total_oos_ly),
        'oos_yoy_pct': round((total_oos_ty-total_oos_ly)/total_oos_ly*100,1) if total_oos_ly>0 else (999.0 if total_oos_ty>0 else 0),
        'snapshot_date': max_inv_d.strftime('%b %d, %Y'),
        'period_start': dc_df['inventory_date'].min().strftime('%b %d'),
        'period_end': max_inv_d.strftime('%b %d, %Y'),
        'dc_count': int(combined['distribution_center_number'].nunique()),
    }

    # By item
    by_item = []
    for itm in ACTIVE_ITEMS:
        s = combined[combined['walmart_item_number']==itm]
        if s.empty: continue
        nm = s['item_name'].iloc[0]
        oh = float(s['on_hand_warehouse_inventory_in_units_this_year'].sum())
        oo = float(s['on_order_warehouse_quantity_in_units_this_year'].sum())
        oh_ly = float(s['on_hand_warehouse_inventory_in_units_last_year'].sum())
        oo_ly = float(s['on_order_warehouse_quantity_in_units_last_year'].sum())
        ws = float(s['weekly_sales'].sum())
        oos_ty_i = float(dc_df[dc_df['walmart_item_number']==itm]['out_of_stock_each_quantity_this_year'].sum())
        oos_ly_i = float(dc_df[dc_df['walmart_item_number']==itm]['out_of_stock_each_quantity_last_year'].sum())
        by_item.append({
            'item_id': int(itm), 'name': nm,
            'on_hand': int(oh), 'on_hand_ly': int(oh_ly),
            'on_hand_yoy_pct': round((oh-oh_ly)/oh_ly*100,1) if oh_ly>0 else 0,
            'on_order': int(oo), 'on_order_ly': int(oo_ly),
            'on_order_yoy_pct': round((oo-oo_ly)/oo_ly*100,1) if oo_ly>0 else 0,
            'total_supply': int(oh+oo),
            'weekly_sales': int(ws),
            'wos_oh': round(oh/ws,1) if ws>0 else 0,
            'wos_total': round((oh+oo)/ws,1) if ws>0 else 0,
            'oos_ty': int(oos_ty_i), 'oos_ly': int(oos_ly_i),
        })

    # Timeline
    ts = dc_df.groupby('inventory_date').agg(
        on_hand=('on_hand_warehouse_inventory_in_units_this_year','sum'),
        on_order=('on_order_warehouse_quantity_in_units_this_year','sum'),
        on_hand_ly=('on_hand_warehouse_inventory_in_units_last_year','sum'),
        on_order_ly=('on_order_warehouse_quantity_in_units_last_year','sum'),
        oos_ty=('out_of_stock_each_quantity_this_year','sum'),
        oos_ly=('out_of_stock_each_quantity_last_year','sum'),
    ).reset_index().sort_values('inventory_date')
    timeline = []
    for r in ts.itertuples():
        _d = pd.Timestamp(r.inventory_date)
        timeline.append({'date':_d.strftime('%Y-%m-%d'),'date_label':_fmt_date(_d),
            'on_hand':int(r.on_hand),'on_order':int(r.on_order),
            'on_hand_ly':int(r.on_hand_ly),'on_order_ly':int(r.on_order_ly),
            'oos_ty':int(r.oos_ty),'oos_ly':int(r.oos_ly)})

    # OOS DCs (cumulative)
    _oos = dc_df[dc_df['out_of_stock_each_quantity_this_year']>0]
    oos_dcs = []
    if len(_oos):
        _has_case = 'out_of_stock_case_quantity_this_year' in _oos.columns
        _agg_args = {
            'oos_days': ('inventory_date','nunique'),
            'oos_units': ('out_of_stock_each_quantity_this_year','sum'),
        }
        if _has_case:
            _agg_args['oos_cases'] = ('out_of_stock_case_quantity_this_year','sum')
        agg = _oos.groupby(['distribution_center_number','name_of_the_dc','walmart_item_number','item_name']).agg(
            **_agg_args
        ).reset_index().sort_values('oos_units', ascending=False)
        for r in agg.itertuples():
            oos_dcs.append({
                'dc_num': int(r.distribution_center_number),
                'dc_name': str(r.name_of_the_dc), 'item': str(r.item_name),
                'oos_days': int(r.oos_days), 'oos_units': int(r.oos_units),
                'oos_cases': int(getattr(r, 'oos_cases', 0)),
            })

    # Critical DCs
    critical = []
    crit_df = combined[(combined['daily_sales']>0) & (combined['days_oh']<7)].copy()
    for r in crit_df.sort_values('daily_sales', ascending=False).itertuples():
        critical.append({
            'dc_num': int(r.distribution_center_number),
            'dc_name': str(r.name_of_the_dc), 'item': str(r.item_name),
            'stores': int(r.stores) if pd.notna(r.stores) else 0,
            'daily_sales': round(float(r.daily_sales),1),
            'on_hand': int(r.on_hand_warehouse_inventory_in_units_this_year),
            'on_order': int(r.on_order_warehouse_quantity_in_units_this_year),
            'days_oh': round(float(r.days_oh),1),
            'days_total': round(float(r.days_total),1),
        })

    # Overstock
    overstock = []
    over_df = combined[(combined['daily_sales']>0) & (combined['days_oh']>30) & (combined['days_oh']<900)].copy()
    for r in over_df.sort_values('on_hand_warehouse_inventory_in_units_this_year', ascending=False).itertuples():
        overstock.append({
            'dc_num': int(r.distribution_center_number),
            'dc_name': str(r.name_of_the_dc), 'item': str(r.item_name),
            'stores': int(r.stores) if pd.notna(r.stores) else 0,
            'daily_sales': round(float(r.daily_sales),1),
            'on_hand': int(r.on_hand_warehouse_inventory_in_units_this_year),
            'on_order': int(r.on_order_warehouse_quantity_in_units_this_year),
            'days_oh': round(float(r.days_oh),1),
        })

    # Full DC × item table
    dc_table = []
    for r in combined.sort_values(['name_of_the_dc','item_name']).itertuples():
        dc_table.append({
            'dc_num': int(r.distribution_center_number),
            'dc_name': str(r.name_of_the_dc), 'item': str(r.item_name),
            'item_id': int(r.walmart_item_number),
            'stores': int(r.stores) if pd.notna(r.stores) else 0,
            'daily_sales': round(float(r.daily_sales),1),
            'on_hand': int(r.on_hand_warehouse_inventory_in_units_this_year),
            'on_hand_ly': int(r.on_hand_warehouse_inventory_in_units_last_year),
            'on_order': int(r.on_order_warehouse_quantity_in_units_this_year),
            'on_order_ly': int(r.on_order_warehouse_quantity_in_units_last_year),
            'wos_oh': round(float(r.wos_oh),1),
            'wos_total': round(float(r.wos_total),1),
            'days_oh': round(float(r.days_oh),1) if r.days_oh<900 else None,
        })

    # VERDICT — data-driven case for/against more inventory
    # Logic: current state drives severity. Historical OOS / on-order trends are
    # added as findings/warnings, not severity bumps unless severe.
    findings = []; severity = 'GREEN'

    # PRIMARY DRIVER: current weeks-of-supply
    if kpi['wos_total'] < 3:
        findings.append({'severity':'red','title':'Total supply below 3 weeks',
            'text':f"Total DC supply (on-hand + on-order) covers only {kpi['wos_total']} weeks at current sales. Below 3-week minimum buffer."})
        severity = 'RED'
    elif kpi['wos_total'] < 5:
        findings.append({'severity':'yellow','title':'Total supply borderline',
            'text':f"Total DC supply covers {kpi['wos_total']} weeks. Acceptable but no margin for demand spikes."})
        if severity == 'GREEN': severity = 'YELLOW'
    else:
        findings.append({'severity':'green','title':'Total supply comfortable',
            'text':f"Total DC supply (on-hand + on-order) covers {kpi['wos_total']} weeks of sales (on-hand: {kpi['wos_oh']}w, on-order: {round((kpi['on_order']/kpi['weekly_sales']) if kpi['weekly_sales']>0 else 0, 1)}w)."})

    # PRIMARY DRIVER: critical DCs running dry
    if len(critical) >= 5:
        crit_stores = sum(c['stores'] for c in critical)
        findings.append({'severity':'red','title':f'{len(critical)} DC/item combos critical now',
            'text':f"{len(critical)} DC/item combinations show <7 days on-hand supply, affecting {crit_stores:,} stores. On-order may close the gap (see Critical DCs table) but timing matters."})
        severity = 'RED'
    elif len(critical) > 0:
        crit_stores = sum(c['stores'] for c in critical)
        findings.append({'severity':'yellow','title':f'{len(critical)} DC/item combos critical now',
            'text':f"{len(critical)} DC/item combos show <7 days on-hand supply, affecting {crit_stores:,} stores. Check on-order coverage."})
        if severity == 'GREEN': severity = 'YELLOW'

    # SECONDARY: pipeline trajectory (downgrade YELLOW only)
    if kpi['on_order_yoy_pct'] <= -40:
        findings.append({'severity':'yellow','title':'On-order severely depleted vs LY',
            'text':f"DC on-order is {kpi['on_order_yoy_pct']:+.1f}% vs LY. With current on-hand at {kpi['on_hand_yoy_pct']:+.1f}% vs LY, the heavier on-hand position is offsetting thinner pipeline — but next reorder cycle needs attention."})
        if severity == 'GREEN': severity = 'YELLOW'
    elif kpi['on_order_yoy_pct'] <= -20:
        findings.append({'severity':'yellow','title':'On-order down vs LY',
            'text':f"DC on-order is {kpi['on_order_yoy_pct']:+.1f}% vs LY. Pipeline thinner — watch for late-period stockouts."})
        if severity == 'GREEN': severity = 'YELLOW'

    # HISTORICAL NOTE: cumulative OOS is informational, not severity-bumping
    # if the current state is healthy. It explains *why* the buyer over-corrected.
    if kpi['oos_ty'] > 0 and kpi['oos_ly'] > 0 and kpi['oos_yoy_pct'] >= 500:
        findings.append({'severity':'yellow','title':'Historical OOS spike vs LY',
            'text':f"This period had {kpi['oos_ty']:,} OOS units vs {kpi['oos_ly']:,} LY ({kpi['oos_yoy_pct']:+.0f}%). Most events were earlier in the period — current on-hand build (+{kpi['on_hand_yoy_pct']:.0f}% vs LY) appears to be the buyer's response. Worth confirming the trajectory has actually corrected."})
        if severity == 'GREEN': severity = 'YELLOW'
    elif kpi['oos_ty'] > 0 and kpi['oos_ly'] == 0:
        findings.append({'severity':'yellow','title':'OOS up from zero LY',
            'text':f"DC OOS totaled {kpi['oos_ty']:,} units vs 0 LY — new stockout problem this year. Confirm current on-hand build is durable."})
        if severity == 'GREEN': severity = 'YELLOW'

    # POSITIVE: overstock = case AGAINST adding inventory blanket
    if len(overstock) >= 10:
        overstock_units = sum(o['on_hand'] for o in overstock)
        findings.append({'severity':'green','title':'Overstock pockets — do not add system-wide',
            'text':f"{len(overstock)} DC/item combos hold >30 days supply ({overstock_units:,} units cushion). System has plenty of inventory in the wrong places. Any additional inventory should be DC-targeted, not blanket."})

    item_verdicts = []
    for it in by_item:
        v_sev = 'green'; v_msg = []
        if it['wos_total'] < 3:
            v_sev = 'red'; v_msg.append(f"Total WOS {it['wos_total']} (need >=3)")
        elif it['wos_total'] < 5:
            v_sev = 'yellow'; v_msg.append(f"Total WOS {it['wos_total']} (target >=5)")
        else:
            v_msg.append(f"WOS {it['wos_total']} — sufficient")
        if it['on_order_yoy_pct'] <= -30:
            if v_sev == 'green': v_sev = 'yellow'
            v_msg.append(f"on-order {it['on_order_yoy_pct']:+.0f}% vs LY")
        if it['oos_ty'] > 0:
            v_msg.append(f"{it['oos_ty']:,} OOS units this period")
        item_verdicts.append({'name':it['name'],'severity':v_sev,'message':' · '.join(v_msg)})

    if severity == 'GREEN':
        rec = ('NO additional DC inventory required system-wide. On-hand + on-order positions are adequate for current sell-through. Address any DC-specific gaps via redistribution rather than new orders.')
    elif severity == 'YELLOW':
        rec = ('NO blanket inventory increase needed — current system supply is sufficient. However, watch the warning signals below: pipeline thinning, isolated critical DCs, or historical OOS patterns suggest the buyer should review reorder quantities for the next cycle, not the current one.')
    else:
        rec = ('ADD DC INVENTORY — system supply is below safe thresholds. Recommended actions: accelerate on-order receipt, prioritize critical DCs, reassess buyer order quantities for next replenishment cycle.')

    verdict = {'severity':severity,'recommendation':rec,'findings':findings,'item_verdicts':item_verdicts}

    return {'kpi':kpi,'by_item':by_item,'timeline':timeline,'oos_dcs':oos_dcs,
            'critical':critical,'overstock':overstock,'dc_table':dc_table,'verdict':verdict}



# ── Prepare all combo datasets ────────────────────────────────────────────────
def prepare_data(df, dc_df=None):
    import sys as _sys
    def _pp(msg):
        print(msg, flush=True); _sys.stdout.flush()
    _pp(' [1/6] Slimming dataframe ...')
    # Slim df to only columns used downstream (major perf win for combo loop)
    _used = ['business_date','_business_date_only','walmart_calendar_week','store_number',
             'item_name','walmart_item_number','state_or_province_code','store_name','city_name',
             'distribution_center_number','name_of_the_dc','storage_distribution_center_number',
             'pos_quantity_this_year','pos_quantity_last_year',
             'store_on_hand_quantity_this_year','store_on_hand_quantity_last_year',
             'store_in_warehouse_quantity_this_year','store_in_transit_quantity_this_year',
             'store_specific_retail_amount_this_year','pos_sales_this_year','pos_sales_last_year']
    df = df[[c for c in _used if c in df.columns]]
    weeks = sorted(df['walmart_calendar_week'].unique())

    _pp(' [2/6] Building daily snapshots ...')
    # ── Pre-compute daily_snapshots ONCE (all items, not per-combo) ───────────
    _ds_store = df.groupby(['business_date','store_number'], sort=False).agg(
        ty=('pos_quantity_this_year','sum'), ly=('pos_quantity_last_year','sum')
    ).reset_index()
    import numpy as _np_ds
    _ds_store['_st'] = _np_ds.where(_ds_store['ty']==0,'Zero',
        _np_ds.where(_ds_store['ty']>_ds_store['ly'],'Up',
        _np_ds.where(_ds_store['ty']<_ds_store['ly'],'Down','Flat')))
    _ds_tot = _ds_store.groupby('business_date', sort=False).agg(
        ty=('ty','sum'), ly=('ly','sum'), total=('store_number','nunique')
    ).reset_index()
    _ds_comp = _ds_store.groupby(['business_date','_st'], sort=False).size().unstack(fill_value=0).reset_index()
    for _sc in ['Up','Down','Flat','Zero']:
        if _sc not in _ds_comp.columns: _ds_comp[_sc]=0
    _ds_comp.columns.name=None
    _ds_m = _ds_tot.merge(_ds_comp[['business_date','Up','Down','Flat','Zero']], on='business_date', how='left')
    daily_snapshots = []
    for _r in _ds_m.sort_values('business_date').itertuples():
        _ts = pd.Timestamp(_r.business_date)
        _yoy = int(_r.ty)-int(_r.ly)
        daily_snapshots.append({'date':_ts.strftime('%Y-%m-%d'),'date_label':_fmt_date(_ts),
            'ty':int(_r.ty),'ly':int(_r.ly),'yoy':_yoy,
            'yoy_pct':round((_yoy/int(_r.ly)*100) if int(_r.ly) else 0,1),
            'total':int(_r.total),'Up':int(getattr(_r,'Up',0)),'Down':int(getattr(_r,'Down',0)),
            'Flat':int(getattr(_r,'Flat',0)),'Zero':int(getattr(_r,'Zero',0))})

    _pp(' [3/6] Computing retail price daily breakdown ...')
    # ── Pre-compute retail_daily ONCE ─────────────────────────────────────────
    _rp_df = df[df['store_specific_retail_amount_this_year']>0].copy()
    _rp_df['bdate'] = _rp_df['business_date'].dt.normalize()
    # Count only selling stores (units > 0) — avoids inflated count on last partial day
    _rp_df['_selling'] = (_rp_df['pos_quantity_this_year'] > 0).astype(int)
    _rp_daily = _rp_df.groupby(['bdate','store_specific_retail_amount_this_year'], sort=False).agg(
        stores=('_selling','sum'),           # selling stores only
        units_ty=('pos_quantity_this_year','sum'),
        units_ly=('pos_quantity_last_year','sum'), sales_ty=('pos_sales_this_year','sum'),
    ).reset_index().sort_values('bdate')
    _all_prices_rp = sorted(_rp_daily['store_specific_retail_amount_this_year'].unique())
    _all_dates_rp  = sorted(_rp_daily['bdate'].unique())
    _rp_map = {(pd.Timestamp(r.bdate).strftime('%Y-%m-%d'), float(r.store_specific_retail_amount_this_year)):r
               for r in _rp_daily.itertuples()}
    retail_daily = []
    for _p in _all_prices_rp:
        _day_rows = []
        for _d in _all_dates_rp:
            _dstr = pd.Timestamp(_d).strftime('%Y-%m-%d')
            _r2 = _rp_map.get((_dstr, float(_p)))
            _s = max(_r2.stores,1) if _r2 else 1
            _day_rows.append({'date':_dstr,'date_label':_fmt_date(pd.Timestamp(_d)),
                'stores':int(_r2.stores) if _r2 else 0, 'units_ty':int(_r2.units_ty) if _r2 else 0,
                'units_ly':int(_r2.units_ly) if _r2 else 0,
                'uspw_ty':round(_r2.units_ty/_s,2) if _r2 else 0.0,
                'uspw_ly':round(_r2.units_ly/_s,2) if _r2 else 0.0,
                'yoy':(int(_r2.units_ty)-int(_r2.units_ly)) if _r2 else 0})
        _curr=_day_rows[-1] if _day_rows else {}; _prev=_day_rows[-2] if len(_day_rows)>=2 else {}
        retail_daily.append({'price':float(_p),'label':f'${_p:.2f}',
            'curr_stores':_curr.get('stores',0),'prev_stores':_prev.get('stores',0),
            'store_delta':_curr.get('stores',0)-_prev.get('stores',0),
            'curr_uspw':_curr.get('uspw_ty',0.0),'days':_day_rows})
    retail_daily.sort(key=lambda x: x['curr_stores'], reverse=True)
    retail_daily = retail_daily[:2]
    retail_date_labels = [_fmt_date(pd.Timestamp(_d)) for _d in _all_dates_rp]

    _pp(' [4/6] Computing store rankings lookups ...')
    # ── Pre-compute shared ranking lookups ONCE on full df ────────────────────
    _max_d_shared = df['business_date'].max()
    _recent_shared = df[df['business_date']==_max_d_shared]
    _oh_shared = _recent_shared.groupby('store_number', sort=False).agg(
        on_hand=('store_on_hand_quantity_this_year','sum'),
        on_hand_ly=('store_on_hand_quantity_last_year','sum'),
        cur_price=('store_specific_retail_amount_this_year','max'),
    ).reset_index()
    _wks_shared = weeks
    _vel_shared = df[df['walmart_calendar_week'].isin(_wks_shared[-4:])].groupby(
        ['store_number','walmart_calendar_week'], sort=False
    ).agg(ty_w=('pos_quantity_this_year','sum'), ly_w=('pos_quantity_last_year','sum')).reset_index()
    _cw_shared = df[df['walmart_calendar_week']==_wks_shared[-1]].groupby(
        'store_number', sort=False
    ).agg(ty_cw=('pos_quantity_this_year','sum'), ly_cw=('pos_quantity_last_year','sum')).reset_index()
    _shared_lookups = {'oh_df':_oh_shared,'vel_df':_vel_shared,'cw_df':_cw_shared}
    items = (df[['walmart_item_number','item_name']].drop_duplicates().sort_values('item_name'))
    item_list=[{'id':int(r.walmart_item_number),'name':r.item_name} for r in items.itertuples()]
    item_ids=[i['id'] for i in item_list]

    # FAST_MODE: skip pre-computing all 2^n - 1 item combinations.
    # The dashboard "All Items" view is built normally; the per-item filter
    # chips fall back to "All Items" when combos aren't pre-computed. This is
    # a 5-10x speedup on slower machines and rarely matters in practice.
    import os as _os
    _fast = _os.environ.get('FUNPOP_FAST_MODE','1') != '0'  # default ON
    combo_data={}
    if not _fast:
        from itertools import combinations as _c2
        _total_combos = sum(1 for _r in range(1, len(item_ids)+1) for _ in _c2(item_ids, _r))
        _done = 0
        print(f' Building per-item combos ({_total_combos} variants) — disable with FUNPOP_FAST_MODE=1')
        for r in range(1, len(item_ids)+1):
            for combo in _combos(item_ids, r):
                _done += 1
                key='_'.join(str(i) for i in sorted(combo))
                print(f'  [{_done}/{_total_combos}] combo {key} ...')
                sub=df[df['walmart_item_number'].isin(combo)]
                try:
                    combo_data[key]=build_core(sub, weeks)
                except Exception as _e:
                    print(f' Warning: combo {key} failed: {_e}')
                    combo_data[key]={}
    else:
        print(' Fast mode ON: skipping per-item combo pre-computation (set FUNPOP_FAST_MODE=0 to enable).')

    _pp(' [5/6] Building main "All Items" view ...')
    try:
        all_data=build_core(df, weeks)
    except Exception as _e:
        print(f' ERROR in build_core: {_e}')
        import traceback; traceback.print_exc()
        all_data={}
    from datetime import datetime
    generated_at = datetime.now().strftime('%B %d, %Y at %I:%M %p')

    # ── DC analysis (optional add-on) ────────────────────────────────────────
    dc_bundle = None
    if dc_df is not None and not dc_df.empty:
        try:
            _pp(' [6/6] Building DC analysis ...')
            dc_bundle = build_dc_analysis(dc_df, df)
        except Exception as _e:
            print(f' Warning: DC analysis failed: {_e}')
            import traceback; traceback.print_exc()
            dc_bundle = None

    return {'all':all_data,'combos':combo_data,'item_list':item_list,'generated_at':generated_at,
            'daily_snapshots':daily_snapshots,'retail_daily':retail_daily,
            'retail_date_labels':retail_date_labels,'dc':dc_bundle}


# ── HTML template ─────────────────────────────────────────────────────────────
HTML = r"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width,initial-scale=1.0">
<title>FunPops Sales and Inventory</title>
<script src="https://cdn.jsdelivr.net/npm/chart.js@4.4.0/dist/chart.umd.min.js"></script>
<link href="https://fonts.googleapis.com/css2?family=DM+Sans:wght@300;400;500&family=DM+Mono:wght@400;500&display=swap" rel="stylesheet">
<style>
*{box-sizing:border-box;margin:0;padding:0;}
body{font-family:'DM Sans',sans-serif;font-size:14px;line-height:1.5;background:#f8f7f4;color:#2C2C2A;min-height:100vh;}
.wrap{max-width:1340px;margin:0 auto;padding:24px 32px;}
.hdr{display:flex;align-items:center;justify-content:space-between;margin-bottom:20px;flex-wrap:wrap;gap:12px;}
.hdr-left .title{font-size:20px;font-weight:500;}
.hdr-left .sub{font-size:12px;color:#5F5E5A;margin-top:2px;}
.yoy-pill .val{font-size:24px;font-weight:500;line-height:1;}
.yoy-pill .lbl{font-size:11px;color:#5F5E5A;text-align:right;}
.filter-bar{display:flex;gap:8px;margin-bottom:16px;flex-wrap:wrap;align-items:center;}
.filter-label{font-size:11px;font-weight:500;text-transform:uppercase;letter-spacing:.07em;color:#888780;margin-right:4px;}
.filter-btn{padding:6px 14px;border-radius:20px;border:0.5px solid #D3D1C7;background:#fff;font-size:12px;font-weight:500;cursor:pointer;color:#5F5E5A;transition:all .15s;user-select:none;}
.filter-btn:hover{border-color:#185FA5;color:#185FA5;}
.filter-btn.active{background:#185FA5;border-color:#185FA5;color:#fff;}
.filter-btn.all-btn.partial{background:#fff;border-color:#185FA5;color:#185FA5;}
.mix-warn{display:none;margin-bottom:16px;padding:10px 14px;background:#FEF9EC;border:0.5px solid #F0C842;border-radius:8px;align-items:flex-start;gap:10px;}
.mix-warn-icon{font-size:16px;flex-shrink:0;}
.mix-warn-title{font-size:12px;font-weight:500;color:#7A5C00;margin-bottom:2px;}
.mix-warn-body{font-size:11px;color:#9A7500;line-height:1.5;}
.kpi-bar{display:grid;grid-template-columns:repeat(4,minmax(0,1fr));border:0.5px solid #D3D1C7;border-radius:10px;overflow:hidden;margin-bottom:22px;background:#fff;}
.kpi-cell{padding:13px 16px;border-right:0.5px solid #D3D1C7;}
.kpi-cell:last-child{border-right:none;}
.kpi-label{font-size:11px;font-weight:500;text-transform:uppercase;letter-spacing:.07em;color:#5F5E5A;margin-bottom:3px;}
.kpi-val{font-size:22px;font-weight:500;line-height:1.1;margin-bottom:1px;}
.kpi-sub{font-size:11px;color:#888780;}
.two{display:grid;grid-template-columns:1fr 1fr;gap:22px;}
.section{margin-bottom:22px;}
.sh{font-size:11px;font-weight:500;text-transform:uppercase;letter-spacing:.08em;color:#5F5E5A;margin-bottom:10px;}
.sh-row{display:flex;align-items:center;justify-content:space-between;margin-bottom:10px;}
.sh-row .sh{margin-bottom:0;}
.badge{font-size:11px;font-weight:500;color:#185FA5;background:#E6F1FB;border:0.5px solid #85B7EB;border-radius:6px;padding:2px 8px;}
.divider{height:0.5px;background:#E8E7E3;margin:18px 0;}
.srow{display:flex;align-items:center;gap:10px;margin-bottom:10px;}
.srow-label{width:56px;font-size:12px;font-weight:500;flex-shrink:0;}
.srow-track{flex:1;height:9px;border-radius:3px;overflow:hidden;background:#F1EFE8;}
.srow-fill{height:100%;border-radius:3px;}
.srow-meta{width:145px;font-size:11px;text-align:right;flex-shrink:0;font-variant-numeric:tabular-nums;color:#5F5E5A;}
.insight{font-size:11px;color:#5F5E5A;background:#F8F7F4;border-radius:6px;padding:8px 10px;border:0.5px solid #D3D1C7;line-height:1.5;}
table{width:100%;border-collapse:collapse;}
th{color:#888780;font-weight:500;font-size:11px;text-transform:uppercase;letter-spacing:.06em;padding:6px 8px;text-align:right;border-bottom:0.5px solid #D3D1C7;white-space:nowrap;}
th:first-child{text-align:left;}
td{padding:7px 8px;text-align:right;border-bottom:0.5px solid #EDECE8;font-variant-numeric:tabular-nums;font-size:12px;}
td:first-child{text-align:left;}
tr:last-child td{border-bottom:none;}
.tfoot td{background:#F8F7F4;font-weight:500;border-top:0.5px solid #D3D1C7;border-bottom:none;}
.cur-col{background:rgba(24,95,165,0.05);}
.cur-th{background:#E6F1FB;color:#185FA5;}
.up{color:#27500A;}.dn{color:#791F1F;}.ze{color:#633806;}.fl{color:#5F5E5A;}
.num-red{color:#A32D2D;font-weight:500;}.num-grn{color:#27500A;font-weight:500;}
.toggle-row{display:flex;gap:6px;}
.toggle-btn{padding:4px 12px;border-radius:6px;border:0.5px solid #D3D1C7;background:#fff;font-size:11px;font-weight:500;cursor:pointer;color:#5F5E5A;}
.toggle-btn.active{background:#185FA5;border-color:#185FA5;color:#fff;}
.inv-item{background:#fff;border:0.5px solid #D3D1C7;border-radius:10px;padding:14px 16px;margin-bottom:12px;}
.inv-name{font-size:13px;font-weight:500;margin-bottom:10px;}
.inv-stats{display:grid;grid-template-columns:repeat(4,1fr);gap:8px;margin-bottom:10px;}
.inv-stat{text-align:center;}
.inv-stat-val{font-size:18px;font-weight:500;line-height:1.1;font-variant-numeric:tabular-nums;}
.inv-stat-sub{font-size:11px;font-variant-numeric:tabular-nums;color:#185FA5;margin-top:1px;font-weight:500;}
.inv-stat-lbl{font-size:10px;color:#888780;text-transform:uppercase;letter-spacing:.06em;margin-top:2px;}
.inv-bar-track{height:6px;background:#F1EFE8;border-radius:3px;overflow:hidden;display:flex;gap:1px;margin-bottom:5px;}
.inv-bar-seg{height:100%;border-radius:2px;}
.inv-legend{display:flex;gap:12px;flex-wrap:wrap;}
.inv-leg-item{display:flex;align-items:center;gap:4px;font-size:10px;color:#888780;}
.inv-leg-dot{width:8px;height:8px;border-radius:2px;}
.wos-badge{display:inline-flex;align-items:center;gap:4px;padding:2px 8px;border-radius:4px;font-size:11px;font-weight:500;}
#rankTable th{cursor:pointer;user-select:none;}
#rankTable th:hover{color:#185FA5;}
.rank-up{color:#27500A;font-weight:500;}
.rank-dn{color:#A32D2D;font-weight:500;}
.rank-fl{color:#888780;}
</style>
</head>
<body>
<div class="wrap">

  <div class="hdr">
    <div class="hdr-left">
      <div class="title">FunPops Sales and Inventory</div>
      <div class="sub" id="subLine"></div>
      <div style="font-size:11px;color:#888780;margin-top:3px;">Last updated: <span id="updatedAt" style="font-weight:500;color:#5F5E5A;"></span></div>
      <div style="margin-top:8px;display:flex;align-items:center;gap:10px;">
        <button id="refreshBtn" onclick="refreshDashboard()" style="padding:6px 14px;border:0.5px solid #185FA5;background:#fff;color:#185FA5;border-radius:6px;font-size:12px;font-weight:500;cursor:pointer;outline:none;">&#x21bb; Refresh now</button>
        <span id="refreshStatus" style="font-size:11px;color:#888780;"></span>
      </div>
    </div>
    <div class="yoy-pill">
      <div class="val" id="hdrYoy"></div>
      <div class="lbl">period YOY</div>
    </div>
  </div>

  <div class="filter-bar">
    <span class="filter-label">Items:</span>
    <button class="filter-btn all-btn active" onclick="toggleAll()">All Items</button>
    <span id="itemBtns"></span>
  </div>

  <!-- Global date filter — sticky so it's always accessible while scrolling -->
  <div id="globalDateBar" style="background:#fff;border-bottom:0.5px solid #D3D1C7;padding:8px 32px;display:flex;align-items:center;gap:10px;position:sticky;top:0;z-index:100;margin:0 -32px 14px -32px;box-shadow:0 1px 4px rgba(0,0,0,0.06);">
    <span class="filter-label" style="margin:0;">Viewing date:</span>
    <select id="globalDatePicker" onchange="setGlobalDate(this.value)"
      style="padding:5px 12px;border:0.5px solid #D3D1C7;border-radius:6px;font-size:12px;font-weight:500;background:#fff;color:#2C2C2A;cursor:pointer;outline:none;min-width:180px;">
    </select>
    <span id="globalDateNote" style="font-size:11px;color:#888780;"></span>
    <span style="margin-left:auto;font-size:11px;color:#B4B2A9;">Scroll date applies to: KPIs &bull; store composition &bull; price point table &bull; section badges</span>
  </div>

  <div class="mix-warn" id="mixWarning">
    <span class="mix-warn-icon">&#9888;</span>
    <div>
      <div class="mix-warn-title">Mixed format selection — interpret blended metrics carefully</div>
      <div class="mix-warn-body">You have selected both a <strong>bin item</strong> and the <strong>shelf item</strong> (36CT 2OZ). Store counts and YOY unit totals are accurate, but blended U/S/W figures may be misleading. Use the <strong>U/S/W by Price Point</strong> section or filter to a single item for velocity comparisons.</div>
    </div>
  </div>

  <div class="kpi-bar" id="kpiBar"></div>

  <div class="two section">
    <div>
      <div class="sh-row">
        <div class="sh">Current week store composition</div>
        <div class="badge" id="compBadge"></div>
      </div>
      <div id="compBars" style="margin-bottom:8px;"></div>
      <div class="insight" id="compInsight"></div>
      <div class="divider"></div>
      <div class="sh">28-day daily units</div>
      <div style="display:flex;gap:14px;margin-bottom:8px;font-size:11px;color:#888780;">
        <span><span style="display:inline-block;width:16px;height:2px;background:#185FA5;vertical-align:middle;margin-right:3px;border-radius:1px;"></span>TY</span>
        <span><span style="display:inline-block;width:16px;height:0;border-top:2px dashed #888780;vertical-align:middle;margin-right:3px;"></span>LY</span>
      </div>
      <div style="position:relative;height:155px;"><canvas id="lineChart"></canvas></div>
    </div>
    <div>
      <div class="sh">Weekly breakdown — all weeks</div>
      <div style="border-radius:10px;overflow:hidden;border:0.5px solid #D3D1C7;margin-bottom:20px;background:#fff;">
        <table style="table-layout:fixed;" id="weeklyTable"></table>
      </div>
      <div class="sh" id="trackerTitle">Last 7 days</div>
      <div style="border-radius:10px;overflow:hidden;border:0.5px solid #D3D1C7;background:#fff;">
        <table id="trackerTable"></table>
      </div>
    </div>
  </div>

  <div class="section">
    <div class="sh">Units / Store / Week — TY vs LY <span id="dateBadge_uspw" style="font-size:11px;font-weight:400;color:#185FA5;background:#E6F1FB;padding:2px 8px;border-radius:4px;margin-left:6px;display:none;"></span></div>
    <div style="border-radius:10px;overflow:hidden;border:0.5px solid #D3D1C7;background:#fff;">
      <table id="uspwTable"></table>
    </div>
  </div>

  <div class="section">
    <div class="sh-row">
      <div style="display:flex;align-items:center;gap:12px;flex-wrap:wrap;">
        <div class="sh" style="margin-bottom:0;">Units &amp; $ / Store / Week &#8212; by retail price point</div>
        <div style="display:flex;align-items:center;gap:6px;">
          <span style="font-size:11px;color:#888780;">Date:</span>
          <select id="ppDatePicker" onchange="setPPDate(this.value)"
            style="padding:4px 8px;border:0.5px solid #D3D1C7;border-radius:6px;font-size:12px;background:#fff;color:#2C2C2A;cursor:pointer;outline:none;"></select>
        </div>
      </div>
      <div class="toggle-row">
        <button class="toggle-btn active" id="ppUnitsBtn" onclick="setPPView('units')">Units</button>
        <button class="toggle-btn" id="ppDollarBtn" onclick="setPPView('dollars')">Dollars</button>
        <button class="toggle-btn" id="ppBothBtn" onclick="setPPView('both')">Both</button>
      </div>
    </div>
      <div class="toggle-row">
        <button class="toggle-btn active" id="ppUnitsBtn" onclick="setPPView('units')">Units</button>
        <button class="toggle-btn" id="ppDollarBtn" onclick="setPPView('dollars')">Dollars</button>
        <button class="toggle-btn" id="ppBothBtn" onclick="setPPView('both')">Both</button>
      </div>
    </div>
    <div style="border-radius:10px;overflow:hidden;border:0.5px solid #D3D1C7;background:#fff;">
      <table id="ppTable"></table>
    </div>
    <div style="font-size:11px;color:#888780;margin-top:6px;">Showing active prices for selected date. U/S/W = daily rate × 7 (weekly equivalent). $0.00 excluded.</div>
  </div>

  <div class="section">
    <div class="sh-row">
      <div class="sh">Inventory snapshot <span id="dateBadge_inv" style="font-size:11px;font-weight:400;color:#185FA5;background:#E6F1FB;padding:2px 8px;border-radius:4px;margin-left:6px;display:none;"></span></div>
      <div class="badge" id="invBadge"></div>
    </div>
    <div id="invGrid"></div>
  </div>

  <div class="section" id="retailPerfSection">
    <div class="sh-row">
      <div class="sh">Retail price — daily migration &amp; performance <span id="dateBadge_retail" style="font-size:11px;font-weight:400;color:#185FA5;background:#E6F1FB;padding:2px 8px;border-radius:4px;margin-left:6px;display:none;"></span></div>
      <div id="retailPerfBadge" class="badge"></div>
    </div>
    <!-- Summary cards — one per active price point -->
    <div id="retailMigrationCards" style="display:grid;grid-template-columns:repeat(auto-fit,minmax(190px,1fr));gap:12px;margin-bottom:18px;"></div>
    <!-- Daily store count by price point -->
    <div style="border:0.5px solid #D3D1C7;border-radius:10px;background:#fff;padding:16px;margin-bottom:16px;">
      <div class="sh" style="margin-bottom:6px;">Stores at each price point — daily</div>
      <div style="font-size:11px;color:#888780;margin-bottom:10px;">Shows price migration day by day. As stores move from one price to another you'll see lines diverge.</div>
      <div id="retailChartLegend" style="display:flex;gap:14px;flex-wrap:wrap;margin-bottom:8px;font-size:11px;color:#888780;"></div>
      <div style="position:relative;height:240px;"><canvas id="retailTrendChart"></canvas></div>
    </div>
    <!-- Daily U/S/W by price point -->
    <div style="border:0.5px solid #D3D1C7;border-radius:10px;background:#fff;padding:16px;margin-bottom:16px;">
      <div class="sh" style="margin-bottom:6px;">Daily units / selling store — by price point</div>
      <div style="font-size:11px;color:#888780;margin-bottom:10px;">Velocity at each price. Higher = price point is driving more purchases per store per day.</div>
      <div id="retailVeloLegend" style="display:flex;gap:14px;flex-wrap:wrap;margin-bottom:8px;font-size:11px;color:#888780;"></div>
      <div style="position:relative;height:200px;"><canvas id="retailVeloChart"></canvas></div>
    </div>
    <!-- Detail table: most recent 14 days per price point -->
    <div style="border:0.5px solid #D3D1C7;border-radius:10px;background:#fff;">
      <table id="retailPerfTable"></table>
    </div>
    <div style="font-size:11px;color:#888780;margin-top:6px;">Daily view. Store delta = vs prior day. $0.00 price points excluded.</div>
  </div>

  <!-- ══════════════════════════════════════════════════════════════════════ -->
  <!-- DC INVENTORY SECTION — only rendered if dc data was loaded                -->
  <!-- ══════════════════════════════════════════════════════════════════════ -->
  <div class="section" id="dcSection" style="display:none;">
    <div class="sh-row" style="margin-bottom:14px;">
      <div class="sh">DC Inventory Analysis &amp; Supply Position</div>
      <div class="badge" id="dcBadge"></div>
    </div>

    <!-- Verdict banner -->
    <div id="dcVerdict" style="border-radius:10px;padding:18px 22px;margin-bottom:18px;border:1px solid;"></div>

    <!-- System KPI bar -->
    <div class="kpi-bar" id="dcKpiBar" style="margin-bottom:18px;"></div>

    <!-- Per-item supply scorecard -->
    <div class="sh" style="margin-top:6px;margin-bottom:10px;">Per-item supply scorecard</div>
    <div id="dcItemCards" style="display:grid;grid-template-columns:repeat(auto-fit,minmax(280px,1fr));gap:14px;margin-bottom:22px;"></div>

    <!-- Findings list -->
    <div class="sh" style="margin-top:6px;margin-bottom:10px;">Findings</div>
    <div id="dcFindings" style="margin-bottom:22px;"></div>

    <!-- Timeline chart -->
    <div style="border:0.5px solid #D3D1C7;border-radius:10px;background:#fff;padding:16px;margin-bottom:18px;">
      <div class="sh" style="margin-bottom:6px;">DC supply trajectory — on-hand &amp; on-order over time</div>
      <div style="font-size:11px;color:#888780;margin-bottom:10px;">Tracks the daily DC inventory build vs LY. Watch for divergence between TY on-order (solid orange) and LY on-order (dashed) — that's the pipeline thinning vs last year.</div>
      <div id="dcTimelineLegend" style="display:flex;gap:14px;flex-wrap:wrap;margin-bottom:8px;font-size:11px;color:#888780;"></div>
      <div style="position:relative;height:260px;"><canvas id="dcTimelineChart"></canvas></div>
    </div>

    <!-- OOS timeline -->
    <div style="border:0.5px solid #D3D1C7;border-radius:10px;background:#fff;padding:16px;margin-bottom:18px;" id="dcOosWrap">
      <div class="sh" style="margin-bottom:6px;">Out-of-stock units — daily</div>
      <div style="font-size:11px;color:#888780;margin-bottom:10px;">Cumulative OOS units this year vs last year. Each bar represents lost shipments to stores.</div>
      <div style="position:relative;height:160px;"><canvas id="dcOosChart"></canvas></div>
    </div>

    <!-- Critical DC table -->
    <div class="sh" style="margin-bottom:8px;">Critical DCs &mdash; less than 7 days on-hand supply <span id="dcCriticalCount" style="font-size:11px;color:#888780;font-weight:400;"></span></div>
    <div style="border-radius:10px;overflow:hidden;border:0.5px solid #D3D1C7;background:#fff;margin-bottom:22px;">
      <table id="dcCriticalTable"></table>
    </div>

    <!-- OOS event log -->
    <div class="sh" style="margin-bottom:8px;">DC OOS event log <span id="dcOosCount" style="font-size:11px;color:#888780;font-weight:400;"></span></div>
    <div style="border-radius:10px;overflow:hidden;border:0.5px solid #D3D1C7;background:#fff;margin-bottom:22px;max-height:380px;overflow-y:auto;" id="dcOosLogWrap">
      <table id="dcOosTable"></table>
    </div>

    <!-- Overstock -->
    <div class="sh" style="margin-bottom:8px;">Overstock pockets &mdash; more than 30 days on-hand <span id="dcOverstockCount" style="font-size:11px;color:#888780;font-weight:400;"></span></div>
    <div style="border-radius:10px;overflow:hidden;border:0.5px solid #D3D1C7;background:#fff;margin-bottom:22px;max-height:380px;overflow-y:auto;">
      <table id="dcOverstockTable"></table>
    </div>

    <!-- Full DC table (sortable) -->
    <div class="sh-row" style="margin-bottom:8px;">
      <div class="sh">Full DC × item table &mdash; sortable</div>
      <input id="dcTableSearch" type="text" placeholder="Search DC..." oninput="renderDcFullTable()"
        style="padding:6px 10px;border:0.5px solid #D3D1C7;border-radius:6px;font-size:12px;width:200px;background:#fff;outline:none;">
    </div>
    <div style="border-radius:10px;overflow:hidden;border:0.5px solid #D3D1C7;background:#fff;max-height:480px;overflow-y:auto;">
      <table id="dcFullTable" style="table-layout:fixed;width:100%;">
        <thead style="position:sticky;top:0;z-index:1;">
          <tr style="background:#F8F7F4;">
            <th onclick="sortDcTable('dc_name')" style="text-align:left;padding:7px 8px;width:200px;cursor:pointer;">DC</th>
            <th onclick="sortDcTable('item')" style="text-align:left;padding:7px 6px;width:140px;cursor:pointer;">Item</th>
            <th onclick="sortDcTable('stores')" style="padding:7px 4px;width:54px;cursor:pointer;">Stores</th>
            <th onclick="sortDcTable('daily_sales')" style="padding:7px 4px;width:64px;cursor:pointer;">Daily Sales</th>
            <th onclick="sortDcTable('on_hand')" style="padding:7px 4px;width:72px;cursor:pointer;">On Hand</th>
            <th onclick="sortDcTable('on_hand_ly')" style="padding:7px 4px;width:72px;cursor:pointer;color:#888780;">OH LY</th>
            <th onclick="sortDcTable('on_order')" style="padding:7px 4px;width:72px;cursor:pointer;">On Order</th>
            <th onclick="sortDcTable('on_order_ly')" style="padding:7px 4px;width:72px;cursor:pointer;color:#888780;">OO LY</th>
            <th onclick="sortDcTable('wos_oh')" style="padding:7px 4px;width:60px;cursor:pointer;">WOS OH</th>
            <th onclick="sortDcTable('wos_total')" style="padding:7px 4px;width:60px;cursor:pointer;">WOS Tot</th>
          </tr>
        </thead>
        <tbody id="dcFullTableBody"></tbody>
      </table>
    </div>
    <div style="font-size:11px;color:#888780;margin-top:6px;">WOS OH = weeks of supply at current sales using on-hand only. WOS Tot = on-hand + on-order. Click any header to sort.</div>
  </div>

  <div class="section">
    <div class="sh-row" style="margin-bottom:10px;">
      <div class="sh">Store ranking — YTD units <span id="dateBadge_rank" style="font-size:11px;font-weight:400;color:#185FA5;background:#E6F1FB;padding:2px 8px;border-radius:4px;margin-left:6px;display:none;"></span></div>
      <div style="font-size:11px;color:#888780;" id="rankCount"></div>
    </div>
    <!-- Filter row -->
    <div style="display:flex;gap:8px;flex-wrap:wrap;margin-bottom:12px;align-items:center;">
      <input id="rankSearchNum" type="text" placeholder="Store #"
        oninput="renderRankings()"
        style="padding:6px 10px;border:0.5px solid #D3D1C7;border-radius:6px;font-size:12px;width:90px;background:#fff;color:#2C2C2A;outline:none;">
      <input id="rankSearchCity" type="text" placeholder="City"
        oninput="renderRankings()"
        style="padding:6px 10px;border:0.5px solid #D3D1C7;border-radius:6px;font-size:12px;width:140px;background:#fff;color:#2C2C2A;outline:none;">
      <select id="rankFilterState" onchange="renderRankings()"
        style="padding:6px 10px;border:0.5px solid #D3D1C7;border-radius:6px;font-size:12px;background:#fff;color:#2C2C2A;cursor:pointer;outline:none;min-width:80px;">
        <option value="">All States</option>
      </select>
      <select id="rankFilterStatus" onchange="renderRankings()"
        style="padding:6px 10px;border:0.5px solid #D3D1C7;border-radius:6px;font-size:12px;background:#fff;color:#2C2C2A;cursor:pointer;outline:none;">
        <option value="">All Status</option>
        <option value="Up">&#9650; Up</option>
        <option value="Down">&#9660; Down</option>
        <option value="Flat">&#8212; Flat</option>
        <option value="Zero">&#9679; Zero</option>
      </select>
      <button onclick="clearRankFilters()"
        style="padding:6px 12px;border:0.5px solid #D3D1C7;border-radius:6px;font-size:12px;background:#fff;color:#888780;cursor:pointer;">
        Clear
      </button>
    </div>
    <div style="border-radius:10px;overflow:hidden;border:0.5px solid #D3D1C7;background:#fff;max-height:560px;overflow-y:auto;">
      <table id="rankTable" style="table-layout:fixed;width:100%;">
        <thead style="position:sticky;top:0;z-index:1;">
          <tr style="background:#F8F7F4;">
            <th onclick="sortRankings('rank_ty')"     style="text-align:center;padding:7px 6px;width:52px;cursor:pointer;">Rank</th>
            <th onclick="sortRankings('rank_chg')"    style="text-align:center;padding:7px 4px;width:46px;cursor:pointer;">Chg</th>
            <th onclick="sortRankings('store')"       style="text-align:left;padding:7px 6px;width:54px;cursor:pointer;">#</th>
            <th onclick="sortRankings('name')"        style="text-align:left;padding:7px 6px;width:180px;cursor:pointer;">Store Name</th>
            <th onclick="sortRankings('city')"        style="text-align:left;padding:7px 6px;width:110px;cursor:pointer;">City</th>
            <th onclick="sortRankings('state')"       style="text-align:left;padding:7px 4px;width:38px;cursor:pointer;">ST</th>
            <th onclick="sortRankings('status')"      style="text-align:center;padding:7px 4px;width:52px;cursor:pointer;">Status</th>
            <th onclick="sortRankings('cur_price')"   style="padding:7px 4px;width:54px;cursor:pointer;">Price</th>
            <th onclick="sortRankings('ty')"          style="padding:7px 6px;width:74px;cursor:pointer;">TY Units</th>
            <th onclick="sortRankings('yoy')"         style="padding:7px 6px;width:74px;cursor:pointer;">YOY Units</th>
            <th onclick="sortRankings('yoy_pct')"     style="padding:7px 4px;width:58px;cursor:pointer;">YOY %</th>
            <th onclick="sortRankings('sales_ty')"    style="padding:7px 6px;width:78px;cursor:pointer;">TY Sales $</th>
            <th onclick="sortRankings('yoy_sales')"   style="padding:7px 6px;width:74px;cursor:pointer;">YOY Sales</th>
            <th onclick="sortRankings('uspd')"        style="padding:7px 4px;width:54px;cursor:pointer;">U/S/Day</th>
            <th onclick="sortRankings('on_hand')"     style="padding:7px 4px;width:60px;cursor:pointer;">On Hand</th>
            <th style="padding:7px 6px;width:80px;text-align:center;">Trend (4wk)</th>
          </tr>
        </thead>
        <tbody id="rankBody"></tbody>
      </table>
    </div>
    <div style="font-size:11px;color:#888780;margin-top:6px;">YTD = full period in file. Status = current week vs LY. Price = most recent day. Click any header to sort.</div>
  </div>


</div>
<script>
const RAW = __DATA__;
let lineChartInst=null;
let globalDate='';
let retailTrendChartInst=null;
let retailVeloChartInst=null;
let ppSelectedDate='';
let stateSortKey='stores';
let stateSortAsc=false;
let stateSearchTerm='';
let selectedIds = ['all'];
let ppView = 'units';
let rankSortKey = 'rank_ty';
let rankSortAsc = true;
let rankSearchTerm = '';

const fmtN  = function(n){ return Math.round(n).toLocaleString(); };
const fmtD  = function(n){ return n.toFixed(2); };
const fmtDollar = function(n){ return '$' + n.toFixed(2); };
const sign  = function(n){ return n > 0 ? '+' : ''; };
const clsN  = function(n){ return n > 0 ? 'num-grn' : n < 0 ? 'num-red' : ''; };
const clsU  = function(n){ return n > 0 ? 'up' : n < 0 ? 'dn' : 'fl'; };
const minus = function(n){ return n < 0 ? '\u2212' : '+'; };

function getComboKey() {
  if (selectedIds.indexOf('all') >= 0 || selectedIds.length === 0) return 'all';
  return selectedIds.slice().sort().join('_');
}
function getBlended() {
  var key = getComboKey();
  return key === 'all' ? RAW.all : (RAW.combos[key] || RAW.all);
}

function buildFilter() {
  var html = RAW.item_list.map(function(item){
    var isActive = selectedIds.indexOf(String(item.id)) >= 0;
    return '<button class="filter-btn' + (isActive ? ' active' : '') + '" onclick="toggleItem(\'' + item.id + '\')">' + item.name + '</button>';
  }).join(' ');
  document.getElementById('itemBtns').innerHTML = html;
  var allSelected = selectedIds.indexOf('all') >= 0;
  var someSelected = !allSelected && selectedIds.length > 0 && selectedIds.length < RAW.item_list.length;
  document.querySelector('.all-btn').className = 'filter-btn all-btn' + (allSelected ? ' active' : (someSelected ? ' partial' : ''));
}

function toggleAll() {
  selectedIds = ['all'];
  buildFilter(); updateMixWarning(); render();
}
function toggleItem(id) {
  id = String(id);
  if (selectedIds.indexOf('all') >= 0) selectedIds = [];
  var idx = selectedIds.indexOf(id);
  if (idx >= 0) selectedIds.splice(idx, 1);
  else selectedIds.push(id);
  if (selectedIds.length === 0 || selectedIds.length === RAW.item_list.length) selectedIds = ['all'];
  buildFilter(); updateMixWarning(); render();
}
function updateMixWarning() {
  var BIN_IDS   = ['658442130','658442128'];
  var SHELF_IDS = ['666209064'];
  var isAll = selectedIds.indexOf('all') >= 0;
  var hasBin   = !isAll && selectedIds.some(function(id){ return BIN_IDS.indexOf(id) >= 0; });
  var hasShelf = !isAll && selectedIds.some(function(id){ return SHELF_IDS.indexOf(id) >= 0; });
  document.getElementById('mixWarning').style.display = (hasBin && hasShelf) ? 'flex' : 'none';
}
function setPPDate(d){ppSelectedDate=d;renderPP(getBlended());}

function setPPView(view) {
  ppView = view;
  document.getElementById('ppUnitsBtn').className  = 'toggle-btn' + (view === 'units'   ? ' active' : '');
  document.getElementById('ppDollarBtn').className = 'toggle-btn' + (view === 'dollars' ? ' active' : '');
  document.getElementById('ppBothBtn').className   = 'toggle-btn' + (view === 'both'    ? ' active' : '');
  renderPP(getBlended());
}

function render() {
  var D = getBlended();
  if (!D || !D.kpi) return;
  var yoyPct = D.kpi.yoy_pct;
  var wks = D.summary.map(function(w){ return w.week; });
  var nameStr = selectedIds.indexOf('all') >= 0 ? 'All Items' :
    selectedIds.map(function(id){ return (RAW.item_list.find(function(i){ return String(i.id)===id; })||{}).name; }).join(' + ');
  document.getElementById('subLine').textContent =
    'Unit performance scorecard \u00b7 ' + wks[0] + ' \u2013 ' + wks[wks.length-1] + ' \u00b7 ' + D.kpi.date_range + ' \u00b7 ' + nameStr;
  document.getElementById('updatedAt').textContent = RAW.generated_at;
  var hdrEl = document.getElementById('hdrYoy');
  hdrEl.textContent = sign(yoyPct) + yoyPct + '%';
  hdrEl.style.color = yoyPct >= 0 ? '#27500A' : '#A32D2D';

  var cc = D.curr_comp;
  var kpiDefs = [
    {label:'TY Units',val:fmtN(D.kpi.ty),sub:'Period total',color:'',bg:''},
    {label:'LY Units',val:fmtN(D.kpi.ly),sub:'Same period',color:'',bg:''},
    {label:'YOY Units',val:minus(D.kpi.yoy)+fmtN(Math.abs(D.kpi.yoy)),
      sub:sign(yoyPct)+yoyPct+'% vs LY',
      color:D.kpi.yoy>=0?'#27500A':'#791F1F',bg:D.kpi.yoy<0?'rgba(252,235,235,0.5)':'rgba(234,243,222,0.5)'},
    {label:'Selling Stores (cur wk)',val:fmtN(cc.total-cc.Zero),
      sub:cc.week+' \u00b7 '+fmtN(cc.total)+' total',color:'',bg:''},
  ];
  document.getElementById('kpiBar').innerHTML = kpiDefs.map(function(k,i){
    var sty = (k.bg ? 'background:'+k.bg+';' : '') + (i===kpiDefs.length-1 ? 'border-right:none;' : '');
    var vSty = k.color ? 'color:'+k.color+';' : '';
    return '<div class="kpi-cell" style="'+sty+'">' +
      '<div class="kpi-label">'+k.label+'</div>' +
      '<div class="kpi-val" style="'+vSty+'">'+k.val+'</div>' +
      '<div class="kpi-sub">'+k.sub+'</div></div>';
  }).join('');

  document.getElementById('compBadge').textContent = cc.week;
  var cats=[{key:'Up',label:'\u25b2 Up',color:'#27500A',bar:'#3B6D11'},
    {key:'Down',label:'\u25bc Down',color:'#791F1F',bar:'#A32D2D'},
    {key:'Zero',label:'\u25cf Zero',color:'#633806',bar:'#BA7517'},
    {key:'Flat',label:'\u2014 Flat',color:'#5F5E5A',bar:'#888780'}];
  document.getElementById('compBars').innerHTML = cats.map(function(c){
    var pct=cc[c.key+'_pct'],cnt=cc[c.key];
    return '<div class="srow"><div class="srow-label" style="color:'+c.color+';">'+c.label+'</div>' +
      '<div class="srow-track"><div class="srow-fill" style="background:'+c.bar+';width:'+pct+'%;min-width:'+(cnt>0?'3':'0')+'px;"></div></div>' +
      '<div class="srow-meta"><strong style="color:'+c.bar+';">'+fmtN(cnt)+'</strong> stores &nbsp;'+pct+'%</div></div>';
  }).join('');
  document.getElementById('compInsight').textContent =
    fmtN(cc.total)+' total stores in '+cc.week+'. '+cc.Down_pct+'% selling below last year. Zero-unit stores at '+cc.Zero_pct+'%.';

  var lastWk=D.summary[D.summary.length-1].week;
  var rowDefs=[{key:'Up',label:'\u25b2 Up',cls:'up',bold:false},{key:'Down',label:'\u25bc Down',cls:'dn',bold:false},
    {key:'Zero',label:'\u25cf Zero',cls:'ze',bold:false},{key:'Flat',label:'\u2014 Flat',cls:'fl',bold:false},
    {key:'Total',label:'Total',cls:'',bold:true}];
  var wtHead='<thead><tr style="background:#F8F7F4;"><th style="text-align:left;padding:6px 8px;width:64px;">Category</th>'+
    D.summary.map(function(w){return '<th class="'+(w.week===lastWk?'cur-th':'')+'" style="padding:6px 4px;">'+w.week.replace('WK ','')+' </th>';}).join('')+'</tr></thead>';
  var wtBody=rowDefs.map(function(r){
    var maxV=Math.max.apply(null,D.summary.map(function(x){return x[r.key]||0;}));
    var cells=D.summary.map(function(w){
      var fw=(w[r.key]===maxV&&r.key!=='Total'&&r.key!=='Flat')?'font-weight:500;':'';
      return '<td class="'+(w.week===lastWk?'cur-col':'')+'" style="padding:7px 4px;'+fw+'">'+fmtN(w[r.key]||0)+'</td>';
    }).join('');
    return '<tr'+(r.bold?' class="tfoot"':'')+'><td style="font-weight:500;padding:7px 8px;" class="'+r.cls+'">'+r.label+'</td>'+cells+'</tr>';
  }).join('');
  document.getElementById('weeklyTable').innerHTML=wtHead+'<tbody>'+wtBody+'</tbody>';

  var t7=D.totals7d;
  var d0=D.tracker7d[D.tracker7d.length-1],d6=D.tracker7d[0];
  document.getElementById('trackerTitle').textContent='Last 7 days  ('+d0.date+' \u2013 '+d6.date+')';
  var maxAbs=Math.max.apply(null,D.tracker7d.map(function(r){return Math.abs(r.yoy);}));
  document.getElementById('trackerTable').innerHTML=
    '<thead><tr style="background:#F8F7F4;"><th style="text-align:left;">Date</th><th style="text-align:left;">Day</th>' +
    '<th>TY</th><th>LY</th><th>YOY</th><th>%</th></tr></thead><tbody>'+
    D.tracker7d.map(function(r){
      var inten=Math.min(0.35,Math.abs(r.yoy)/maxAbs*0.35).toFixed(2);
      var bg=r.yoy<0?'rgba(243,123,123,'+inten+')':r.yoy>0?'rgba(100,196,130,'+inten+')':'';
      return '<tr style="background:'+bg+';"><td style="font-weight:500;">'+r.date+'</td>' +
        '<td style="color:#5F5E5A;">'+r.day+'</td><td>'+fmtN(r.ty)+'</td><td>'+fmtN(r.ly)+'</td>' +
        '<td class="'+clsN(r.yoy)+'">'+minus(r.yoy)+fmtN(Math.abs(r.yoy))+'</td>' +
        '<td class="'+clsN(r.yoy)+'">'+sign(r.yoy_pct)+r.yoy_pct+'%</td></tr>';
    }).join('') +
    '<tr class="tfoot"><td colspan="2">7-day total</td><td>'+fmtN(t7.ty)+'</td><td>'+fmtN(t7.ly)+'</td>' +
    '<td class="'+clsN(t7.yoy)+'">'+minus(t7.yoy)+fmtN(Math.abs(t7.yoy))+'</td>' +
    '<td class="'+clsN(t7.yoy)+'">'+sign(t7.pct)+t7.pct+'%</td></tr></tbody>';

  document.getElementById('uspwTable').innerHTML=
    '<thead><tr style="background:#F8F7F4;"><th style="text-align:left;padding:7px 12px;">Week</th>' +
    '<th>TY U/S/W (All)</th><th>LY U/S/W (All)</th><th>TY U/S/W (Selling)</th><th>LY U/S/W (Selling)</th>' +
    '<th>Selling Stores TY</th><th>Total Stores</th></tr></thead><tbody>'+
    D.uspw.map(function(r,i){
      var isLast=i===D.uspw.length-1,yoyAll=r.ty_all-r.ly_all,yoySell=r.ty_sell-r.ly_sell;
      var sty=isLast?'background:rgba(24,95,165,0.03);font-weight:500;':'';
      return '<tr style="'+sty+'"><td style="font-weight:500;padding:7px 12px;">'+r.week+(isLast?' \u2605':'')+'</td>' +
        '<td class="'+clsU(yoyAll)+'">'+fmtD(r.ty_all)+'</td><td style="color:#888780;">'+fmtD(r.ly_all)+'</td>' +
        '<td class="'+clsU(yoySell)+'">'+fmtD(r.ty_sell)+'</td><td style="color:#888780;">'+fmtD(r.ly_sell)+'</td>' +
        '<td>'+fmtN(r.sell_ty)+'</td><td style="color:#888780;">'+fmtN(r.total_s)+'</td></tr>';
    }).join('')+'</tbody>';

  renderPP(D); renderInventory(D); renderRetailPerf(D); renderRankings(); renderDc();

  var labels=D.chart4w.map(function(d){var dt=new Date(d.date+'T00:00:00');return (dt.getMonth()+1)+'/'+dt.getDate();});
  if(lineChartInst){lineChartInst.destroy();lineChartInst=null;}
  lineChartInst=new Chart(document.getElementById('lineChart'),{type:'line',
    data:{labels:labels,datasets:[
      {label:'TY',data:D.chart4w.map(function(d){return d.ty;}),borderColor:'#185FA5',backgroundColor:'rgba(24,95,165,0.07)',borderWidth:2,pointRadius:0,tension:0.3,fill:true},
      {label:'LY',data:D.chart4w.map(function(d){return d.ly;}),borderColor:'#B4B2A9',borderDash:[4,3],borderWidth:1.5,pointRadius:0,tension:0.3},
    ]},
    options:{responsive:true,maintainAspectRatio:false,interaction:{mode:'index',intersect:false},
      plugins:{legend:{display:false},tooltip:{backgroundColor:'#fff',titleColor:'#2C2C2A',bodyColor:'#5F5E5A',
        borderColor:'#D3D1C7',borderWidth:1,padding:8,
        callbacks:{label:function(c){return ' '+c.dataset.label+': '+Math.round(c.parsed.y).toLocaleString()+' units';}}}},
      scales:{x:{grid:{display:false},ticks:{color:'#888780',font:{size:10},maxTicksLimit:7}},
        y:{grid:{color:'rgba(0,0,0,0.04)'},ticks:{color:'#888780',font:{size:10},callback:function(v){return (v/1000).toFixed(0)+'K';}}}}}
  });

  var _pd=D.pp_date_list||[],_pk=document.getElementById('ppDatePicker');
  if(_pk){var _pc=ppSelectedDate||D.latest_pp_date||(_pd.length?_pd[_pd.length-1]:'');
    _pk.innerHTML=_pd.slice().reverse().map(function(d){var dt=new Date(d+'T00:00:00');
      return '<option value="'+d+'"'+(d===_pc?' selected':'')+'>'+dt.toLocaleDateString('en-US',{month:'short',day:'numeric',year:'numeric'})+'</option>';
    }).join('');ppSelectedDate=_pc;}
  // Global date filter: init from most recent snapshot, then render date-specific sections
  if(!globalDate){
    var _snaps=RAW.daily_snapshots||[];
    if(_snaps.length) globalDate=_snaps[_snaps.length-1].date;
  }
  populateGlobalDatePicker();
  renderGlobalDateSections(D);

}

function renderPP(D) {
  var _ppByDate=D.uspw_pp_by_date||{},_ppSel=ppSelectedDate||D.latest_pp_date||'';
  var pp=_ppByDate[_ppSel]||D.uspw_pp||[];
  if(!pp||pp.length===0){document.getElementById('ppTable').innerHTML='<tbody><tr><td style="padding:12px;color:#888780;">No data.</td></tr></tbody>';return;}
  var showU=ppView==='units'||ppView==='both',showD=ppView==='dollars'||ppView==='both';
  var head='<thead><tr style="background:#F8F7F4;"><th style="text-align:left;padding:7px 12px;">Price Point</th><th>Stores</th>'+
    (showU?'<th>TY U/S/W</th><th>LY U/S/W</th><th>U/S/W YOY</th>':'')+
    (showD?'<th>TY $/S/W</th><th>LY $/S/W</th><th>$/S/W YOY</th>':'')+
    '<th>TY Total Units</th><th>LY Total Units</th></tr></thead>';
  var body=pp.map(function(p,i){
    var uYoy=p.uspw_ty-p.uspw_ly,sYoy=p.spw_ty-p.spw_ly;
    return '<tr style="'+(i%2===1?'background:#FAFAF8;':'')+'">' +
      '<td style="padding:7px 12px;font-weight:500;">'+p.label+'</td><td>'+(p.stores!=null?p.stores.toLocaleString():'--')+'</td>'+
      (showU?'<td class="'+clsU(uYoy)+'">'+fmtD(p.uspw_ty)+'</td><td style="color:#888780;">'+fmtD(p.uspw_ly)+'</td><td class="'+clsN(uYoy)+'">'+(uYoy>=0?'+':'\u2212')+Math.abs(uYoy).toFixed(2)+'</td>':'')+
      (showD?'<td class="'+clsU(sYoy)+'">'+fmtDollar(p.spw_ty)+'</td><td style="color:#888780;">'+fmtDollar(p.spw_ly)+'</td><td class="'+clsN(sYoy)+'">'+(sYoy>=0?'+$':'\u2212$')+Math.abs(sYoy).toFixed(2)+'</td>':'')+
      '<td>'+p.units_ty.toLocaleString()+'</td><td style="color:#888780;">'+p.units_ly.toLocaleString()+'</td></tr>';
  }).join('');
  document.getElementById('ppTable').innerHTML=head+'<tbody>'+body+'</tbody>';
}

function renderInventory(D) {
  document.getElementById('invBadge').textContent=D.inv_date;
  document.getElementById('invGrid').innerHTML=D.inventory.map(function(r){
    var total=r.on_hand_ty+r.warehouse+r.in_transit;
    var ohPct=total?Math.round(r.on_hand_ty/total*100):0;
    var whPct=total?Math.round(r.warehouse/total*100):0;
    var itPct=total?Math.round(r.in_transit/total*100):0;
    var ohYoy=r.on_hand_ty-r.on_hand_ly;
    var wosCls=r.wos<2?'#A32D2D':r.wos<4?'#854F0B':'#27500A';
    var wosBg=r.wos<2?'rgba(252,235,235,0.6)':r.wos<4?'rgba(250,238,218,0.6)':'rgba(234,243,222,0.6)';
    return '<div class="inv-item">' +
      '<div style="display:flex;align-items:center;justify-content:space-between;margin-bottom:10px;">' +
        '<div class="inv-name">'+r.item+'</div>' +
        '<div style="display:flex;align-items:center;gap:8px;">' +
          '<div style="font-size:11px;color:#888780;">'+r.container_units+' units/'+r.container_label.slice(0,-1)+'</div>' +
          '<div class="wos-badge" style="color:'+wosCls+';background:'+wosBg+';">'+r.wos+'x WOS</div>' +
        '</div></div>' +
      '<div class="inv-stats">' +
        '<div class="inv-stat"><div class="inv-stat-val" style="color:#185FA5;">'+fmtN(r.on_hand_ty)+'</div>' +
          '<div class="inv-stat-sub">'+r.on_hand_cont.toLocaleString()+' '+r.container_label+'</div>' +
          '<div class="inv-stat-lbl">On Hand TY</div>' +
          '<div style="font-size:10px;margin-top:2px;" class="'+clsN(ohYoy)+'">'+minus(ohYoy)+fmtN(Math.abs(ohYoy))+' vs LY</div></div>' +
        '<div class="inv-stat"><div class="inv-stat-val" style="color:#5BA3D9;">'+fmtN(r.warehouse)+'</div>' +
          '<div class="inv-stat-sub">'+r.warehouse_cont.toLocaleString()+' '+r.container_label+'</div>' +
          '<div class="inv-stat-lbl">Warehouse</div></div>' +
        '<div class="inv-stat"><div class="inv-stat-val" style="color:#7BAFD4;">'+fmtN(r.in_transit)+'</div>' +
          '<div class="inv-stat-sub">'+r.in_transit_cont.toLocaleString()+' '+r.container_label+'</div>' +
          '<div class="inv-stat-lbl">In Transit</div></div>' +
        '<div class="inv-stat"><div class="inv-stat-val">'+fmtN(r.total_supply)+'</div>' +
          '<div class="inv-stat-sub">'+r.total_cont.toLocaleString()+' '+r.container_label+'</div>' +
          '<div class="inv-stat-lbl">Total Supply</div></div>' +
      '</div>' +
      '<div class="inv-bar-track">' +
        '<div class="inv-bar-seg" style="background:#185FA5;width:'+ohPct+'%;"></div>' +
        '<div class="inv-bar-seg" style="background:#5BA3D9;width:'+whPct+'%;"></div>' +
        '<div class="inv-bar-seg" style="background:#A8D0EE;width:'+itPct+'%;"></div>' +
      '</div>' +
      '<div class="inv-legend">' +
        '<div class="inv-leg-item"><div class="inv-leg-dot" style="background:#185FA5;"></div>On Hand '+ohPct+'%</div>' +
        '<div class="inv-leg-item"><div class="inv-leg-dot" style="background:#5BA3D9;"></div>Warehouse '+whPct+'%</div>' +
        '<div class="inv-leg-item"><div class="inv-leg-dot" style="background:#A8D0EE;"></div>In Transit '+itPct+'%</div>' +
        '<div class="inv-leg-item" style="margin-left:auto;font-size:10px;color:#888780;">'+r.stores.toLocaleString()+' stores</div>' +
      '</div></div>';
  }).join('');
}


function clearRankFilters() {
  ['rankSearchNum','rankSearchCity'].forEach(function(id){ var el=document.getElementById(id); if(el) el.value=''; });
  ['rankFilterState','rankFilterStatus'].forEach(function(id){ var el=document.getElementById(id); if(el) el.value=''; });
  renderRankings();
}

function populateRankStateFilter(rows) {
  var sel=document.getElementById('rankFilterState'); if(!sel) return;
  var cur=sel.value, states=[];
  rows.forEach(function(r){ if(r.state&&states.indexOf(r.state)<0) states.push(r.state); });
  states.sort();
  sel.innerHTML='<option value="">All States</option>'+
    states.map(function(s){return '<option value="'+s+'"'+(s===cur?' selected':'')+'>'+s+'</option>';}).join('');
}

function sortRankings(key) {
  if(rankSortKey===key) rankSortAsc=!rankSortAsc;
  else { rankSortKey=key; rankSortAsc=(key==='rank_ty'); }
  renderRankings();
}

function renderRankings() {
  var allRows=(getBlended().rankings||[]);
  populateRankStateFilter(allRows);
  var rows=allRows.slice();

  var numQ=((document.getElementById('rankSearchNum')||{}).value||'').trim();
  if(numQ) rows=rows.filter(function(r){ return String(r.store).indexOf(numQ)>=0; });

  var cityQ=((document.getElementById('rankSearchCity')||{}).value||'').toLowerCase();
  if(cityQ) rows=rows.filter(function(r){ return (r.city||'').toLowerCase().indexOf(cityQ)>=0; });

  var stateQ=(document.getElementById('rankFilterState')||{}).value||'';
  if(stateQ) rows=rows.filter(function(r){ return r.state===stateQ; });

  var statusQ=(document.getElementById('rankFilterStatus')||{}).value||'';
  if(statusQ) rows=rows.filter(function(r){ return r.status===statusQ; });

  rows.sort(function(a,b){
    var av=a[rankSortKey],bv=b[rankSortKey];
    if(av==null&&bv==null) return 0; if(av==null) return 1; if(bv==null) return -1;
    if(typeof av==='string') return rankSortAsc?av.localeCompare(bv):bv.localeCompare(av);
    return rankSortAsc?av-bv:bv-av;
  });

  var countEl=document.getElementById('rankCount');
  if(countEl) {
    var _rsnaps=RAW.daily_snapshots||[];
    var _rsnap=_rsnaps.find(function(s){return s.date===globalDate;});
    var _rdlbl=_rsnap?new Date(_rsnap.date+'T00:00:00').toLocaleDateString('en-US',{month:'short',day:'numeric',year:'numeric'}):'';
    countEl.innerHTML=rows.length.toLocaleString()+' of '+allRows.length.toLocaleString()+' stores'+
      (_rdlbl?' &nbsp;<span style="font-size:11px;color:#888780;">●&nbsp;YTD through '+_rdlbl+'</span>':'')+
      ' &nbsp;<span style="font-size:11px;color:#888780;">●&nbsp;Status = current week vs LY</span>';
  }

  var SS={'Up':{cls:'up',icon:'\u25b2',bg:'rgba(234,243,222,0.6)'},'Down':{cls:'dn',icon:'\u25bc',bg:'rgba(252,235,235,0.5)'},
    'Flat':{cls:'fl',icon:'\u2014',bg:''},'Zero':{cls:'ze',icon:'\u25cf',bg:'rgba(250,238,218,0.5)'}};

  document.getElementById('rankBody').innerHTML=rows.map(function(r,i){
    var chgCls=r.rank_chg>0?'rank-up':r.rank_chg<0?'rank-dn':'rank-fl';
    var chgStr=r.rank_chg>0?('+'+r.rank_chg+' \u25b2'):r.rank_chg<0?(r.rank_chg+' \u25bc'):'\u2014';
    var ss=SS[r.status]||SS['Flat'];
    var bg=i%2===1?'background:#FAFAF8;':'';
    var ohYoy=(r.on_hand||0)-(r.on_hand_ly||0);
    var trendDots=(r.trend||[]).slice(0,4).reverse().map(function(w){
      var c=w.dir==='Up'?'#27500A':w.dir==='Down'?'#A32D2D':w.dir==='Zero'?'#BA7517':'#B4B2A9';
      return '<span style="display:inline-block;width:9px;height:9px;border-radius:50%;background:'+c+';margin:0 1px;" title="TY '+w.ty+' / LY '+w.ly+'"></span>';
    }).join('');
    return '<tr style="'+bg+'">' +
      '<td style="text-align:center;font-weight:600;font-size:13px;padding:7px 6px;">'+r.rank_ty+'</td>' +
      '<td style="text-align:center;padding:7px 4px;" class="'+chgCls+'">'+chgStr+'</td>' +
      '<td style="text-align:left;color:#888780;padding:7px 6px;font-size:11px;font-family:monospace;">'+r.store+'</td>' +
      '<td style="text-align:left;font-weight:500;padding:7px 6px;white-space:nowrap;overflow:hidden;text-overflow:ellipsis;" title="'+r.name+'">'+r.name+'</td>' +
      '<td style="text-align:left;padding:7px 6px;font-size:12px;color:#5F5E5A;white-space:nowrap;overflow:hidden;text-overflow:ellipsis;" title="'+r.city+'">'+r.city+'</td>' +
      '<td style="text-align:left;padding:7px 4px;font-size:12px;">'+r.state+'</td>' +
      '<td style="text-align:center;padding:4px 3px;"><span style="display:inline-block;padding:2px 5px;border-radius:4px;font-size:10px;font-weight:500;background:'+ss.bg+';" class="'+ss.cls+'">'+ss.icon+' '+r.status+'</span></td>' +
      '<td style="padding:7px 4px;font-size:12px;">'+(r.cur_price?'$'+r.cur_price.toFixed(2):'--')+'</td>' +
      '<td style="padding:7px 6px;">'+r.ty.toLocaleString()+'</td>' +
      '<td style="padding:7px 6px;" class="'+clsN(r.yoy)+'">'+(r.yoy>=0?'+':'\u2212')+Math.abs(r.yoy).toLocaleString()+'</td>' +
      '<td style="padding:7px 4px;" class="'+clsN(r.yoy_pct)+'">'+(r.yoy_pct>=0?'+':'')+r.yoy_pct+'%</td>' +
      '<td style="padding:7px 6px;font-size:12px;">$'+(r.sales_ty||0).toLocaleString(undefined,{maximumFractionDigits:0})+'</td>' +
      '<td style="padding:7px 6px;" class="'+clsN(r.yoy_sales)+'">'+(r.yoy_sales>=0?'+$':'\u2212$')+Math.abs(r.yoy_sales||0).toLocaleString(undefined,{maximumFractionDigits:0})+'</td>' +
      '<td style="padding:7px 4px;font-size:12px;">'+((r.uspd||0).toFixed(1))+'</td>' +
      '<td style="padding:7px 4px;" class="'+clsN(ohYoy)+'" title="LY: '+(r.on_hand_ly||0).toLocaleString()+'">'+((r.on_hand||0).toLocaleString())+'</td>' +
      '<td style="padding:6px 6px;text-align:center;">'+trendDots+'</td>' +
      '</tr>';
  }).join('');
}







function renderRetailPerf(D) {
  var rd = (D.retail_daily || RAW.retail_daily || []).filter(function(r){ return r.price > 0; });
  if (!rd.length) return;

  var COLORS = ['#185FA5','#A32D2D','#27500A','#854F0B','#633806','#378ADD','#C44F4F','#3B6D11'];

  // ── Summary cards ─────────────────────────────────────────────────────────
  var badgeEl = document.getElementById('retailPerfBadge');
  if (badgeEl) {
    var latestDate = rd[0] && rd[0].days.length ? rd[0].days[rd[0].days.length-1].date : '';
    badgeEl.textContent = latestDate ? new Date(latestDate+'T00:00:00').toLocaleDateString('en-US',{month:'short',day:'numeric',year:'numeric'}) : '';
  }

  document.getElementById('retailMigrationCards').innerHTML = rd.map(function(r, i) {
    var clr = COLORS[i % COLORS.length];
    var dCls = r.store_delta > 0 ? 'num-grn' : r.store_delta < 0 ? 'num-red' : 'rank-fl';
    var dArrow = r.store_delta > 0 ? '\u25b2' : r.store_delta < 0 ? '\u25bc' : '\u2014';
    var curr = r.days[r.days.length-1] || {};
    var uspwDir = curr.uspw_ty > curr.uspw_ly ? 'num-grn' : curr.uspw_ty < curr.uspw_ly ? 'num-red' : '';
    // Sparkline: last 14 days of store count
    var last14 = r.days.slice(-14);
    var maxS = Math.max.apply(null, last14.map(function(d){ return d.stores; })) || 1;
    var spark = last14.map(function(d, j) {
      var h = Math.round(d.stores / maxS * 28);
      var x = j * 7 + 2;
      return '<rect x="'+x+'" y="'+(30-h)+'" width="5" height="'+h+'" rx="1" fill="'+clr+'44"/>';
    }).join('');
    return '<div style="border:0.5px solid '+clr+';border-radius:10px;padding:14px;background:#fff;">' +
      '<div style="display:flex;align-items:center;justify-content:space-between;margin-bottom:6px;">' +
        '<div style="font-size:20px;font-weight:500;color:'+clr+';">'+r.label+'</div>' +
        '<div class="'+dCls+'" style="font-size:12px;font-weight:500;">'+dArrow+' '+Math.abs(r.store_delta)+' vs prev day</div>' +
      '</div>' +
      '<div style="font-size:28px;font-weight:500;line-height:1;">'+r.curr_stores.toLocaleString()+'</div>' +
      '<div style="font-size:11px;color:#888780;margin-bottom:10px;">stores today</div>' +
      '<svg width="100" height="32" style="display:block;margin-bottom:8px;">'+spark+'</svg>' +
      '<div style="border-top:0.5px solid #EDECE8;padding-top:8px;display:grid;grid-template-columns:1fr 1fr;gap:4px;">' +
        '<div style="font-size:11px;color:#888780;">U/S/Day TY</div>' +
        '<div style="font-size:11px;font-weight:500;text-align:right;" class="'+uspwDir+'">'+
          (curr.uspw_ty != null ? curr.uspw_ty.toFixed(2) : '--')+'</div>' +
        '<div style="font-size:11px;color:#888780;">U/S/Day LY</div>' +
        '<div style="font-size:11px;color:#888780;text-align:right;">'+
          (curr.uspw_ly != null ? curr.uspw_ly.toFixed(2) : '--')+'</div>' +
        '<div style="font-size:11px;color:#888780;">YOY Units</div>' +
        '<div style="font-size:11px;font-weight:500;text-align:right;" class="'+clsN(curr.yoy)+'">'+
          (curr.yoy >= 0 ? '+' : '\u2212')+Math.abs(curr.yoy || 0).toLocaleString()+'</div>' +
      '</div>' +
    '</div>';
  }).join('');

  // ── Store count trend chart ───────────────────────────────────────────────
  if (retailTrendChartInst) { retailTrendChartInst.destroy(); retailTrendChartInst = null; }
  var labels = RAW.retail_date_labels || (rd[0] ? rd[0].days.map(function(d){ return d.date_label; }) : []);
  var legendEl = document.getElementById('retailChartLegend');
  if (legendEl) legendEl.innerHTML = rd.map(function(r,i){
    var clr=COLORS[i%COLORS.length];
    return '<span><span style="display:inline-block;width:14px;height:3px;background:'+clr+';vertical-align:middle;margin-right:4px;border-radius:2px;"></span>'+r.label+'</span>';
  }).join('');

  retailTrendChartInst = new Chart(document.getElementById('retailTrendChart'), {type:'line',
    data:{
      labels: labels,
      datasets: rd.map(function(r,i){
        var clr=COLORS[i%COLORS.length];
        return {label:r.label,
          data:r.days.map(function(d){ return d.stores > 0 ? d.stores : null; }),
          borderColor:clr, borderWidth:2, pointRadius:0, pointHoverRadius:4,
          pointBackgroundColor:clr, tension:0.25, spanGaps:false,
          fill:false};
      }),
    },
    options:{responsive:true,maintainAspectRatio:false,
      interaction:{mode:'index',intersect:false},
      plugins:{legend:{display:false},
        tooltip:{backgroundColor:'#fff',titleColor:'#2C2C2A',bodyColor:'#5F5E5A',
          borderColor:'#D3D1C7',borderWidth:1,padding:10,
          callbacks:{label:function(c){
            var r=rd[c.datasetIndex]; var d=r?r.days[c.dataIndex]:null;
            if(!d||!d.stores) return ' '+c.dataset.label+': no stores';
            return ' '+c.dataset.label+': '+d.stores.toLocaleString()+' stores';
          }}}},
      scales:{
        y:{grid:{color:'rgba(0,0,0,0.04)'},ticks:{color:'#888780',font:{size:10}},
          title:{display:true,text:'Stores',color:'#888780',font:{size:10}}},
        x:{grid:{display:false},ticks:{color:'#888780',font:{size:10},maxTicksLimit:14}}
      }}
  });

  // ── Velocity (U/S/Day) chart ──────────────────────────────────────────────
  if (retailVeloChartInst) { retailVeloChartInst.destroy(); retailVeloChartInst = null; }
  var veloLegEl = document.getElementById('retailVeloLegend');
  if (veloLegEl) veloLegEl.innerHTML = rd.map(function(r,i){
    var clr=COLORS[i%COLORS.length];
    return '<span><span style="display:inline-block;width:14px;height:3px;background:'+clr+';vertical-align:middle;margin-right:4px;border-radius:2px;"></span>'+r.label+'</span>';
  }).join('');

  retailVeloChartInst = new Chart(document.getElementById('retailVeloChart'), {type:'line',
    data:{
      labels: labels,
      datasets: rd.map(function(r,i){
        var clr=COLORS[i%COLORS.length];
        return {label:r.label,
          data:r.days.map(function(d){ return d.stores > 0 ? d.uspw_ty : null; }),
          borderColor:clr, borderWidth:2, pointRadius:0, pointHoverRadius:4,
          pointBackgroundColor:clr, tension:0.3, spanGaps:false, fill:false};
      }),
    },
    options:{responsive:true,maintainAspectRatio:false,
      interaction:{mode:'index',intersect:false},
      plugins:{legend:{display:false},
        tooltip:{backgroundColor:'#fff',titleColor:'#2C2C2A',bodyColor:'#5F5E5A',
          borderColor:'#D3D1C7',borderWidth:1,padding:10,
          callbacks:{label:function(c){
            var r=rd[c.datasetIndex]; var d=r?r.days[c.dataIndex]:null;
            if(!d||!d.stores) return ' '+c.dataset.label+': no stores';
            return ' '+c.dataset.label+': '+d.uspw_ty.toFixed(2)+' u/s/day  (LY: '+d.uspw_ly.toFixed(2)+')';
          }}}},
      scales:{
        y:{grid:{color:'rgba(0,0,0,0.04)'},ticks:{color:'#888780',font:{size:10}},
          title:{display:true,text:'Units / Store / Day',color:'#888780',font:{size:10}}},
        x:{grid:{display:false},ticks:{color:'#888780',font:{size:10},maxTicksLimit:14}}
      }}
  });

  // ── Detail table: last 14 days, one row per price point ──────────────────
  var last14 = (rd[0] ? rd[0].days.slice(-14) : []);
  var tblDates = last14.map(function(d){ return d.date_label; });
  var head = '<thead><tr style="background:#F8F7F4;">' +
    '<th style="text-align:left;padding:7px 12px;min-width:70px;">Price</th>' +
    '<th style="padding:7px 8px;">Now</th>' +
    '<th style="padding:7px 8px;">\u0394 Day</th>' +
    tblDates.map(function(dl,i){
      var isLast = i===tblDates.length-1;
      return '<th style="padding:7px 6px;font-size:10px;min-width:52px;'+(isLast?'background:#E6F1FB;color:#185FA5;':'')+'">'+dl+'</th>';
    }).join('') +
    '</tr></thead>';

  var body = '<tbody>' + rd.map(function(r, ri) {
    var clr = COLORS[ri%COLORS.length];
    var dCls = r.store_delta>0?'num-grn':r.store_delta<0?'num-red':'rank-fl';
    var dArrow = r.store_delta>0?'\u25b2':r.store_delta<0?'\u25bc':'\u2014';
    var dayCells = last14.map(function(_, di) {
      var d = r.days[r.days.length - 14 + di];
      var isLast = di === last14.length-1;
      var bg = isLast ? 'background:rgba(24,95,165,0.05);' : (di%2===1?'background:#FAFAF8;':'');
      if (!d || !d.stores) return '<td style="padding:7px 6px;font-size:11px;color:#D3D1C7;'+bg+'">—</td>';
      return '<td style="padding:7px 6px;font-size:11px;'+bg+'">'+d.stores.toLocaleString()+'</td>';
    }).join('');
    return '<tr>' +
      '<td style="padding:7px 12px;font-weight:500;color:'+clr+';">'+r.label+'</td>' +
      '<td style="padding:7px 8px;font-weight:500;">'+r.curr_stores.toLocaleString()+'</td>' +
      '<td style="padding:7px 8px;" class="'+dCls+'">'+dArrow+' '+Math.abs(r.store_delta)+'</td>' +
      dayCells +
      '</tr>';
  }).join('') + '</tbody>';

  document.getElementById('retailPerfTable').innerHTML = head + body;
}


// ══════════════════════════════════════════════════════════════════════════════
// DC INVENTORY RENDERING
// ══════════════════════════════════════════════════════════════════════════════
var dcTimelineChartInst = null;
var dcOosChartInst = null;
var dcTableSortKey = 'wos_oh';
var dcTableSortAsc = true;

function renderDc() {
  var dc = RAW.dc;
  var sec = document.getElementById('dcSection');
  if (!dc || !dc.kpi) { sec.style.display = 'none'; return; }
  sec.style.display = '';

  // ── Snapshot badge ────────────────────────────────────────────────────────
  document.getElementById('dcBadge').textContent = dc.kpi.snapshot_date + ' · ' + dc.kpi.dc_count + ' DCs';

  // ── Verdict banner ────────────────────────────────────────────────────────
  var v = dc.verdict;
  var sevColors = {
    'GREEN':  {bg:'#EAF3DE', border:'#3B6D11', text:'#27500A', icon:'✓'},
    'YELLOW': {bg:'#FEF9EC', border:'#F0C842', text:'#7A5C00', icon:'⚠'},
    'RED':    {bg:'#FCEBEB', border:'#A32D2D', text:'#791F1F', icon:'✕'},
  };
  var sc = sevColors[v.severity] || sevColors['YELLOW'];
  document.getElementById('dcVerdict').style.background = sc.bg;
  document.getElementById('dcVerdict').style.borderColor = sc.border;
  document.getElementById('dcVerdict').style.color = sc.text;
  document.getElementById('dcVerdict').innerHTML =
    '<div style="display:flex;align-items:flex-start;gap:14px;">' +
      '<div style="font-size:28px;line-height:1;">' + sc.icon + '</div>' +
      '<div style="flex:1;">' +
        '<div style="font-size:11px;font-weight:500;text-transform:uppercase;letter-spacing:.08em;margin-bottom:4px;">Recommendation · ' + v.severity + '</div>' +
        '<div style="font-size:14px;line-height:1.5;font-weight:500;">' + v.recommendation + '</div>' +
      '</div>' +
    '</div>';

  // ── System KPI bar ────────────────────────────────────────────────────────
  var k = dc.kpi;
  var kpiDefs = [
    {label:'DC On-Hand', val:fmtN(k.on_hand), sub:'LY ' + fmtN(k.on_hand_ly) + ' (' + (k.on_hand_yoy_pct>=0?'+':'') + k.on_hand_yoy_pct + '%)',
     color: k.on_hand_yoy_pct >= 0 ? '#27500A' : '#791F1F'},
    {label:'DC On-Order', val:fmtN(k.on_order), sub:'LY ' + fmtN(k.on_order_ly) + ' (' + (k.on_order_yoy_pct>=0?'+':'') + k.on_order_yoy_pct + '%)',
     color: k.on_order_yoy_pct >= 0 ? '#27500A' : (k.on_order_yoy_pct <= -20 ? '#791F1F' : '#7A5C00')},
    {label:'Total Supply', val:fmtN(k.total_supply), sub:fmtN(k.weekly_sales) + ' weekly sales',
     color:''},
    {label:'Weeks of Supply', val:k.wos_total.toFixed(1) + 'w', sub:'On-hand only: ' + k.wos_oh.toFixed(1) + 'w',
     color: k.wos_total < 3 ? '#791F1F' : (k.wos_total < 5 ? '#7A5C00' : '#27500A')},
  ];
  document.getElementById('dcKpiBar').innerHTML = kpiDefs.map(function(d, i){
    var vSty = d.color ? 'color:' + d.color + ';' : '';
    var sty = i === kpiDefs.length - 1 ? 'border-right:none;' : '';
    return '<div class="kpi-cell" style="' + sty + '">' +
      '<div class="kpi-label">' + d.label + '</div>' +
      '<div class="kpi-val" style="' + vSty + '">' + d.val + '</div>' +
      '<div class="kpi-sub">' + d.sub + '</div></div>';
  }).join('');

  // ── Per-item supply scorecard ─────────────────────────────────────────────
  var ivColors = {'green':{bg:'#EAF3DE',border:'#3B6D11',text:'#27500A'},
                  'yellow':{bg:'#FEF9EC',border:'#F0C842',text:'#7A5C00'},
                  'red':{bg:'#FCEBEB',border:'#A32D2D',text:'#791F1F'}};
  document.getElementById('dcItemCards').innerHTML = dc.by_item.map(function(it, i){
    var iv = (v.item_verdicts || [])[i] || {severity:'green', message:''};
    var sc2 = ivColors[iv.severity] || ivColors['green'];
    var ohYoyCls = it.on_hand_yoy_pct >= 0 ? 'num-grn' : 'num-red';
    var ooYoyCls = it.on_order_yoy_pct >= 0 ? 'num-grn' : 'num-red';
    return '<div style="border:1px solid ' + sc2.border + ';border-radius:10px;background:#fff;padding:14px;">' +
      '<div style="display:flex;justify-content:space-between;align-items:center;margin-bottom:10px;">' +
        '<div style="font-size:13px;font-weight:500;">' + it.name + '</div>' +
        '<div style="font-size:10px;font-weight:500;text-transform:uppercase;letter-spacing:.06em;padding:2px 8px;border-radius:4px;background:' + sc2.bg + ';color:' + sc2.text + ';">' + iv.severity.toUpperCase() + '</div>' +
      '</div>' +
      '<div style="display:grid;grid-template-columns:1fr 1fr;gap:8px;margin-bottom:10px;">' +
        '<div><div style="font-size:10px;color:#888780;text-transform:uppercase;letter-spacing:.06em;">On Hand</div>' +
          '<div style="font-size:18px;font-weight:500;">' + fmtN(it.on_hand) + '</div>' +
          '<div style="font-size:10px;" class="' + ohYoyCls + '">' + (it.on_hand_yoy_pct>=0?'+':'') + it.on_hand_yoy_pct + '% vs LY</div></div>' +
        '<div><div style="font-size:10px;color:#888780;text-transform:uppercase;letter-spacing:.06em;">On Order</div>' +
          '<div style="font-size:18px;font-weight:500;">' + fmtN(it.on_order) + '</div>' +
          '<div style="font-size:10px;" class="' + ooYoyCls + '">' + (it.on_order_yoy_pct>=0?'+':'') + it.on_order_yoy_pct + '% vs LY</div></div>' +
      '</div>' +
      '<div style="display:grid;grid-template-columns:1fr 1fr;gap:8px;padding-top:10px;border-top:0.5px solid #E8E7E3;">' +
        '<div><div style="font-size:10px;color:#888780;">Weeks Supply</div>' +
          '<div style="font-size:14px;font-weight:500;color:' + sc2.text + ';">' + it.wos_total.toFixed(1) + 'w total</div>' +
          '<div style="font-size:10px;color:#888780;">' + it.wos_oh.toFixed(1) + 'w on-hand</div></div>' +
        '<div><div style="font-size:10px;color:#888780;">Period OOS</div>' +
          '<div style="font-size:14px;font-weight:500;' + (it.oos_ty>0?'color:#791F1F':'') + '">' + fmtN(it.oos_ty) + ' units</div>' +
          '<div style="font-size:10px;color:#888780;">LY: ' + fmtN(it.oos_ly) + '</div></div>' +
      '</div>' +
      '<div style="font-size:11px;color:#5F5E5A;margin-top:10px;line-height:1.5;">' + iv.message + '</div>' +
    '</div>';
  }).join('');

  // ── Findings list ─────────────────────────────────────────────────────────
  document.getElementById('dcFindings').innerHTML = (v.findings || []).map(function(f){
    var fc = ivColors[f.severity] || ivColors['yellow'];
    return '<div style="border-left:3px solid ' + fc.border + ';background:' + fc.bg + ';padding:10px 14px;border-radius:0 6px 6px 0;margin-bottom:8px;">' +
      '<div style="font-size:12px;font-weight:500;color:' + fc.text + ';margin-bottom:3px;">' + f.title + '</div>' +
      '<div style="font-size:12px;color:#2C2C2A;line-height:1.5;">' + f.text + '</div></div>';
  }).join('');

  // ── Timeline chart ────────────────────────────────────────────────────────
  if (dcTimelineChartInst) { dcTimelineChartInst.destroy(); dcTimelineChartInst = null; }
  var tl = dc.timeline || [];
  document.getElementById('dcTimelineLegend').innerHTML =
    '<span><span style="display:inline-block;width:14px;height:3px;background:#185FA5;vertical-align:middle;margin-right:4px;border-radius:2px;"></span>On-Hand TY</span>' +
    '<span><span style="display:inline-block;width:14px;height:0;border-top:2px dashed #185FA5;vertical-align:middle;margin-right:4px;"></span>On-Hand LY</span>' +
    '<span><span style="display:inline-block;width:14px;height:3px;background:#BA7517;vertical-align:middle;margin-right:4px;border-radius:2px;"></span>On-Order TY</span>' +
    '<span><span style="display:inline-block;width:14px;height:0;border-top:2px dashed #BA7517;vertical-align:middle;margin-right:4px;"></span>On-Order LY</span>';
  dcTimelineChartInst = new Chart(document.getElementById('dcTimelineChart'), {
    type: 'line',
    data: {
      labels: tl.map(function(d){ return d.date_label; }),
      datasets: [
        {label:'On-Hand TY', data:tl.map(function(d){return d.on_hand;}),
         borderColor:'#185FA5', backgroundColor:'rgba(24,95,165,0.06)',
         borderWidth:2, pointRadius:0, tension:0.25, fill:true},
        {label:'On-Hand LY', data:tl.map(function(d){return d.on_hand_ly;}),
         borderColor:'#185FA5', borderDash:[4,3], borderWidth:1.5, pointRadius:0, tension:0.25},
        {label:'On-Order TY', data:tl.map(function(d){return d.on_order;}),
         borderColor:'#BA7517', borderWidth:2, pointRadius:0, tension:0.25},
        {label:'On-Order LY', data:tl.map(function(d){return d.on_order_ly;}),
         borderColor:'#BA7517', borderDash:[4,3], borderWidth:1.5, pointRadius:0, tension:0.25},
      ],
    },
    options:{responsive:true,maintainAspectRatio:false,
      interaction:{mode:'index',intersect:false},
      plugins:{legend:{display:false},
        tooltip:{backgroundColor:'#fff',titleColor:'#2C2C2A',bodyColor:'#5F5E5A',
          borderColor:'#D3D1C7',borderWidth:1,padding:10,
          callbacks:{label:function(c){return ' ' + c.dataset.label + ': ' + Math.round(c.parsed.y).toLocaleString() + ' units';}}}},
      scales:{
        y:{grid:{color:'rgba(0,0,0,0.04)'},ticks:{color:'#888780',font:{size:10},
          callback:function(val){return (val/1000).toFixed(0) + 'K';}}},
        x:{grid:{display:false},ticks:{color:'#888780',font:{size:10},maxTicksLimit:12}}
      }}
  });

  // ── OOS chart ─────────────────────────────────────────────────────────────
  var hasOos = tl.some(function(d){ return d.oos_ty>0 || d.oos_ly>0; });
  document.getElementById('dcOosWrap').style.display = hasOos ? '' : 'none';
  if (hasOos) {
    if (dcOosChartInst) { dcOosChartInst.destroy(); dcOosChartInst = null; }
    dcOosChartInst = new Chart(document.getElementById('dcOosChart'), {
      type: 'bar',
      data: {
        labels: tl.map(function(d){ return d.date_label; }),
        datasets: [
          {label:'OOS TY', data:tl.map(function(d){return d.oos_ty;}),
           backgroundColor:'#A32D2D', borderRadius:2},
          {label:'OOS LY', data:tl.map(function(d){return d.oos_ly;}),
           backgroundColor:'#B4B2A9', borderRadius:2},
        ],
      },
      options:{responsive:true,maintainAspectRatio:false,
        plugins:{legend:{display:true,position:'top',labels:{font:{size:10},color:'#5F5E5A'}},
          tooltip:{backgroundColor:'#fff',titleColor:'#2C2C2A',bodyColor:'#5F5E5A',borderColor:'#D3D1C7',borderWidth:1}},
        scales:{
          y:{grid:{color:'rgba(0,0,0,0.04)'},ticks:{color:'#888780',font:{size:10}},
            title:{display:true,text:'OOS units',color:'#888780',font:{size:10}}},
          x:{grid:{display:false},ticks:{color:'#888780',font:{size:10},maxTicksLimit:12}}
        }}
    });
  }

  // ── Critical DCs ─────────────────────────────────────────────────────────
  var crit = dc.critical || [];
  document.getElementById('dcCriticalCount').textContent = '(' + crit.length + ')';
  if (crit.length === 0) {
    document.getElementById('dcCriticalTable').innerHTML = '<tbody><tr><td style="padding:14px;color:#888780;">No DCs running below 7 days on-hand supply right now.</td></tr></tbody>';
  } else {
    document.getElementById('dcCriticalTable').innerHTML =
      '<thead><tr style="background:#F8F7F4;"><th style="text-align:left;padding:7px 12px;">DC</th>' +
      '<th style="text-align:left;padding:7px 6px;">Item</th><th>Stores</th><th>Daily Sales</th>' +
      '<th>On Hand</th><th>On Order</th><th>Days OH</th><th>Days Total</th></tr></thead><tbody>' +
      crit.map(function(c,i){
        var sty = i%2===1?'background:#FAFAF8;':'';
        var ohCls = c.days_oh<=2?'num-red':'num-grn';
        var totCls = c.days_total<7?'num-red':(c.days_total<14?'':'num-grn');
        return '<tr style="' + sty + '">' +
          '<td style="padding:7px 12px;font-weight:500;">' + c.dc_name + ' <span style="color:#888780;font-size:10px;">#' + c.dc_num + '</span></td>' +
          '<td style="text-align:left;padding:7px 6px;font-size:12px;">' + c.item + '</td>' +
          '<td>' + c.stores.toLocaleString() + '</td>' +
          '<td>' + c.daily_sales.toFixed(1) + '</td>' +
          '<td>' + c.on_hand.toLocaleString() + '</td>' +
          '<td>' + c.on_order.toLocaleString() + '</td>' +
          '<td class="' + ohCls + '">' + c.days_oh.toFixed(1) + '</td>' +
          '<td class="' + totCls + '">' + c.days_total.toFixed(1) + '</td></tr>';
      }).join('') + '</tbody>';
  }

  // ── OOS log ──────────────────────────────────────────────────────────────
  var oosDcs = dc.oos_dcs || [];
  document.getElementById('dcOosCount').textContent = '(' + oosDcs.length + ' DC×item combinations)';
  if (oosDcs.length === 0) {
    document.getElementById('dcOosLogWrap').style.display = 'none';
  } else {
    document.getElementById('dcOosLogWrap').style.display = '';
    document.getElementById('dcOosTable').innerHTML =
      '<thead><tr style="background:#F8F7F4;position:sticky;top:0;z-index:1;"><th style="text-align:left;padding:7px 12px;">DC</th>' +
      '<th style="text-align:left;padding:7px 6px;">Item</th><th>OOS Days</th><th>OOS Units</th><th>OOS Cases</th></tr></thead><tbody>' +
      oosDcs.map(function(o,i){
        return '<tr style="' + (i%2===1?'background:#FAFAF8;':'') + '">' +
          '<td style="padding:7px 12px;font-weight:500;">' + o.dc_name + ' <span style="color:#888780;font-size:10px;">#' + o.dc_num + '</span></td>' +
          '<td style="text-align:left;padding:7px 6px;font-size:12px;">' + o.item + '</td>' +
          '<td>' + o.oos_days + '</td>' +
          '<td class="num-red">' + o.oos_units.toLocaleString() + '</td>' +
          '<td>' + o.oos_cases.toLocaleString() + '</td></tr>';
      }).join('') + '</tbody>';
  }

  // ── Overstock ────────────────────────────────────────────────────────────
  var over = dc.overstock || [];
  document.getElementById('dcOverstockCount').textContent = '(' + over.length + ')';
  if (over.length === 0) {
    document.getElementById('dcOverstockTable').innerHTML = '<tbody><tr><td style="padding:14px;color:#888780;">No DCs holding over 30 days supply.</td></tr></tbody>';
  } else {
    document.getElementById('dcOverstockTable').innerHTML =
      '<thead><tr style="background:#F8F7F4;position:sticky;top:0;z-index:1;"><th style="text-align:left;padding:7px 12px;">DC</th>' +
      '<th style="text-align:left;padding:7px 6px;">Item</th><th>Stores</th><th>Daily Sales</th>' +
      '<th>On Hand</th><th>On Order</th><th>Days OH</th></tr></thead><tbody>' +
      over.map(function(o,i){
        return '<tr style="' + (i%2===1?'background:#FAFAF8;':'') + '">' +
          '<td style="padding:7px 12px;font-weight:500;">' + o.dc_name + ' <span style="color:#888780;font-size:10px;">#' + o.dc_num + '</span></td>' +
          '<td style="text-align:left;padding:7px 6px;font-size:12px;">' + o.item + '</td>' +
          '<td>' + o.stores.toLocaleString() + '</td>' +
          '<td>' + o.daily_sales.toFixed(1) + '</td>' +
          '<td>' + o.on_hand.toLocaleString() + '</td>' +
          '<td>' + o.on_order.toLocaleString() + '</td>' +
          '<td style="color:#7A5C00;font-weight:500;">' + o.days_oh.toFixed(0) + '</td></tr>';
      }).join('') + '</tbody>';
  }

  // ── Full sortable DC table ───────────────────────────────────────────────
  renderDcFullTable();
}

function sortDcTable(key) {
  if (dcTableSortKey === key) dcTableSortAsc = !dcTableSortAsc;
  else { dcTableSortKey = key; dcTableSortAsc = (key === 'dc_name' || key === 'item'); }
  renderDcFullTable();
}

function renderDcFullTable() {
  var dc = RAW.dc;
  if (!dc) return;
  var rows = (dc.dc_table || []).slice();
  var q = ((document.getElementById('dcTableSearch')||{}).value||'').toLowerCase();
  if (q) rows = rows.filter(function(r){
    return (r.dc_name||'').toLowerCase().indexOf(q) >= 0 ||
           (r.item||'').toLowerCase().indexOf(q) >= 0 ||
           String(r.dc_num).indexOf(q) >= 0;
  });
  rows.sort(function(a,b){
    var av=a[dcTableSortKey], bv=b[dcTableSortKey];
    if (av==null && bv==null) return 0;
    if (av==null) return 1; if (bv==null) return -1;
    if (typeof av === 'string') return dcTableSortAsc ? av.localeCompare(bv) : bv.localeCompare(av);
    return dcTableSortAsc ? av-bv : bv-av;
  });
  document.getElementById('dcFullTableBody').innerHTML = rows.map(function(r,i){
    var sty = i%2===1 ? 'background:#FAFAF8;' : '';
    var ohYoy = r.on_hand - r.on_hand_ly;
    var ooYoy = r.on_order - r.on_order_ly;
    var wosOhCls = r.wos_oh < 1 ? 'num-red' : (r.wos_oh < 3 ? '' : 'num-grn');
    var wosTotCls = r.wos_total < 3 ? 'num-red' : (r.wos_total < 5 ? '' : 'num-grn');
    return '<tr style="' + sty + '">' +
      '<td style="text-align:left;padding:7px 8px;font-weight:500;font-size:12px;white-space:nowrap;overflow:hidden;text-overflow:ellipsis;" title="' + r.dc_name + '">' + r.dc_name + '</td>' +
      '<td style="text-align:left;padding:7px 6px;font-size:11px;white-space:nowrap;overflow:hidden;text-overflow:ellipsis;" title="' + r.item + '">' + r.item + '</td>' +
      '<td style="padding:7px 4px;">' + r.stores.toLocaleString() + '</td>' +
      '<td style="padding:7px 4px;">' + r.daily_sales.toFixed(1) + '</td>' +
      '<td style="padding:7px 4px;">' + r.on_hand.toLocaleString() + '</td>' +
      '<td style="padding:7px 4px;color:#888780;">' + r.on_hand_ly.toLocaleString() + '</td>' +
      '<td style="padding:7px 4px;">' + r.on_order.toLocaleString() + '</td>' +
      '<td style="padding:7px 4px;color:#888780;">' + r.on_order_ly.toLocaleString() + '</td>' +
      '<td style="padding:7px 4px;" class="' + wosOhCls + '">' + r.wos_oh.toFixed(1) + '</td>' +
      '<td style="padding:7px 4px;" class="' + wosTotCls + '">' + r.wos_total.toFixed(1) + '</td>' +
      '</tr>';
  }).join('');
}


function populateGlobalDatePicker() {
  var D = getBlended();
  var snaps = RAW.daily_snapshots || [];
  if (!snaps.length) return;
  var picker = document.getElementById('globalDatePicker');
  if (!picker) return;
  // Most recent first
  var opts = snaps.slice().reverse();
  picker.innerHTML = opts.map(function(s) {
    var dt = new Date(s.date + 'T00:00:00');
    var lbl = dt.toLocaleDateString('en-US', {weekday:'short', month:'short', day:'numeric', year:'numeric'});
    return '<option value="' + s.date + '"' + (s.date === globalDate ? ' selected' : '') + '>' + lbl + '</option>';
  }).join('');
  // Default to most recent
  if (!globalDate || !snaps.find(function(s){return s.date===globalDate;})) {
    globalDate = snaps[snaps.length-1].date;
    picker.value = globalDate;
  }
}

function setGlobalDate(d) {
  globalDate = d;
  // Sync pp date picker to global date
  var ppPicker = document.getElementById('ppDatePicker');
  if (ppPicker) {
    ppPicker.value = d;
    ppSelectedDate = d;
  }
  renderGlobalDateSections(getBlended());
}

function renderGlobalDateSections(D) {
  var snaps = (D && D.daily_snapshots) || [];
  var snap = snaps.find(function(s){ return s.date === globalDate; });
  if (!snap) snap = snaps[snaps.length-1];
  if (!snap) return;

  var noteEl = document.getElementById('globalDateNote');
  if (noteEl) {
    // Show delta from most recent if not most recent
    var isLatest = snap.date === snaps[snaps.length-1].date;
    noteEl.textContent = isLatest ? 'Most recent data' : 'Viewing historical date';
  }

  // ── Re-render KPI bar with selected day ────────────────────────────────────
  var yoyPct = snap.yoy_pct;
  var hdrEl = document.getElementById('hdrYoy');
  if (hdrEl) {
    hdrEl.textContent = (yoyPct >= 0 ? '+' : '') + yoyPct + '%';
    hdrEl.style.color = yoyPct >= 0 ? '#27500A' : '#A32D2D';
  }
  var cc = snap;
  var ct = Math.max(cc.total, 1);
  var kpiDefs = [
    {label:'TY Units', val:fmtN(snap.ty), sub:'Units sold this day', color:'', bg:''},
    {label:'LY Units', val:fmtN(snap.ly), sub:'Same day last year', color:'', bg:''},
    {label:'YOY Units', val:(snap.yoy>=0?'+':'\u2212')+fmtN(Math.abs(snap.yoy)),
      sub:(yoyPct>=0?'+':'')+yoyPct+'% vs LY',
      color:snap.yoy>=0?'#27500A':'#791F1F',
      bg:snap.yoy<0?'rgba(252,235,235,0.5)':'rgba(234,243,222,0.5)'},
    {label:'Selling Stores', val:fmtN(cc.total - cc.Zero),
      sub:'of '+fmtN(cc.total)+' total stores', color:'', bg:''},
  ];
  document.getElementById('kpiBar').innerHTML = kpiDefs.map(function(k,i){
    var sty=(k.bg?'background:'+k.bg+';':'')+(i===kpiDefs.length-1?'border-right:none;':'');
    var vSty=k.color?'color:'+k.color+';':'';
    return '<div class="kpi-cell" style="'+sty+'">'+
      '<div class="kpi-label">'+k.label+'</div>'+
      '<div class="kpi-val" style="'+vSty+'">'+k.val+'</div>'+
      '<div class="kpi-sub">'+k.sub+'</div></div>';
  }).join('');

  // ── Re-render store composition bars ──────────────────────────────────────
  var compBadge = document.getElementById('compBadge');
  if (compBadge) {
    var dt2 = new Date(snap.date+'T00:00:00');
    compBadge.textContent = dt2.toLocaleDateString('en-US',{month:'short',day:'numeric'});
  }
  var cats=[{key:'Up',label:'\u25b2 Up',color:'#27500A',bar:'#3B6D11'},
    {key:'Down',label:'\u25bc Down',color:'#791F1F',bar:'#A32D2D'},
    {key:'Zero',label:'\u25cf Zero',color:'#633806',bar:'#BA7517'},
    {key:'Flat',label:'\u2014 Flat',color:'#5F5E5A',bar:'#888780'}];
  document.getElementById('compBars').innerHTML = cats.map(function(c){
    var cnt=snap[c.key]||0, pct=Math.round(cnt/ct*100);
    return '<div class="srow"><div class="srow-label" style="color:'+c.color+';">'+c.label+'</div>'+
      '<div class="srow-track"><div class="srow-fill" style="background:'+c.bar+';width:'+pct+'%;min-width:'+(cnt>0?'3':'0')+'px;"></div></div>'+
      '<div class="srow-meta"><strong style="color:'+c.bar+';">'+fmtN(cnt)+'</strong> stores &nbsp;'+pct+'%</div></div>';
  }).join('');
  var insEl = document.getElementById('compInsight');
  if (insEl) insEl.textContent = fmtN(snap.total)+' stores on this day. '+
    Math.round((snap.Down||0)/ct*100)+'% selling below last year. Zero-unit stores at '+
    Math.round((snap.Zero||0)/ct*100)+'%.';

  // ── Sync price point picker ────────────────────────────────────────────────
  renderPP(D);

  // ── Update section date badges ───────────────────────────────────────────
  var dt3 = new Date(snap.date+'T00:00:00');
  var badgeLbl = dt3.toLocaleDateString('en-US',{weekday:'short',month:'short',day:'numeric'});
  var isLatest2 = snap.date === snaps[snaps.length-1].date;
  var badgeIds = ['dateBadge_uspw','dateBadge_inv','dateBadge_retail','dateBadge_rank'];
  badgeIds.forEach(function(id){
    var el = document.getElementById(id);
    if (!el) return;
    el.textContent = badgeLbl;
    el.style.display = '';
    el.style.background = isLatest2 ? '#E6F1FB' : '#FEF9EC';
    el.style.color      = isLatest2 ? '#185FA5' : '#7A5C00';
  });

  // ── Re-render rankings with date context ─────────────────────────────────
  renderRankings();
}

buildFilter(); updateMixWarning(); render();

// ── Refresh button ─────────────────────────────────────────────────────────
var REFRESH_URL = "__REFRESH_URL__";
function refreshDashboard() {
  var btn = document.getElementById('refreshBtn');
  var status = document.getElementById('refreshStatus');
  if (!REFRESH_URL || REFRESH_URL.indexOf('__') === 0) {
    status.textContent = 'Refresh URL not configured.';
    status.style.color = '#791F1F';
    return;
  }
  btn.disabled = true;
  btn.style.opacity = '0.6';
  status.textContent = 'Refreshing...';
  status.style.color = '#185FA5';
  fetch(REFRESH_URL, {method: 'POST'})
    .then(function(r){ return r.json().then(function(j){ return {status: r.status, body: j}; }); })
    .then(function(res){
      if (res.status === 200) {
        status.textContent = 'Done. Reload page in ~10s to see new data.';
        status.style.color = '#27500A';
      } else if (res.status === 429) {
        status.textContent = (res.body && res.body.message) || 'Cooldown: try again later.';
        status.style.color = '#7A5C00';
      } else {
        status.textContent = 'Refresh failed: ' + ((res.body && res.body.message) || res.status);
        status.style.color = '#791F1F';
      }
    })
    .catch(function(e){
      status.textContent = 'Network error: ' + e.message;
      status.style.color = '#791F1F';
    })
    .finally(function(){
      btn.disabled = false;
      btn.style.opacity = '1';
    });
}

</script>
</body>
</html>"""

def _sanitize(obj):
    """Recursively replace float nan/inf with None so json.dumps produces valid JSON."""
    import math
    if isinstance(obj, float):
        return None if (math.isnan(obj) or math.isinf(obj)) else obj
    if isinstance(obj, dict):
        return {k: _sanitize(v) for k, v in obj.items()}
    if isinstance(obj, (list, tuple)):
        return [_sanitize(v) for v in obj]
    return obj

def build_html(data, output_path=None, refresh_url=''):
    """Build dashboard HTML. If output_path is a Path, writes to disk and returns the path.
    If output_path is None, returns the HTML string (used by the Cloud Function)."""
    html = HTML.replace('__DATA__', json.dumps(_sanitize(data)))
    html = html.replace('__REFRESH_URL__', refresh_url or '')
    if output_path is None:
        return html
    output_path.write_text(html, encoding='utf-8')
    return output_path
