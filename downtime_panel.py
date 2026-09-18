"""Read-only downtime dashboard; record creation stays in the application."""
from datetime import date, timedelta
import html

import pandas as pd
import plotly.graph_objects as go
import streamlit as st


def prepare_records(records):
    rows = records.copy()
    rows['duration'] = pd.to_numeric(rows['duration'], errors='coerce').fillna(0).clip(lower=0)
    rows['_time'] = pd.to_datetime(rows['event_at'], errors='coerce')
    hours = rows['_time'].dt.hour
    rows['Vardiya'] = hours.map(lambda h: 'Bilinmiyor' if pd.isna(h) else ('Sabah' if 8 <= h < 16 else ('Akşam' if h >= 16 else 'Gece')))
    rows['reason'] = rows['reason'].fillna('Belirtilmemiş')
    return rows


def render_downtime_panel(query, machines):
    rows = prepare_records(query('SELECT id,machine_code,reason,duration,start_time,end_time,notes,event_at FROM downtime ORDER BY id DESC'))
    st.markdown('''<style>
    .dt-kpi{background:linear-gradient(120deg,#fff,#effaf6);border:1px solid #d4eae1;border-radius:10px;padding:14px;min-height:95px;box-sizing:border-box}
    .dt-kpi small{display:block;color:#466659;font-size:12px;line-height:18px}
    .dt-kpi b{display:block;color:#075b41;font-size:24px;line-height:32px;font-variant-numeric:tabular-nums}
    .dt-heading{color:#124f3c;font-size:14px;font-weight:700;margin-bottom:12px}
    .dt-bar{display:grid;grid-template-columns:70px minmax(0,1fr) 58px;align-items:center;gap:10px;margin:12px 0;font-size:12px;color:#254f40}
    .dt-track{height:9px;border-radius:8px;background:#e7f2ed;overflow:hidden}.dt-track i{display:block;height:100%;border-radius:8px;background:#10966a}
    .dt-days{display:grid;grid-template-columns:repeat(auto-fit,minmax(65px,1fr));gap:6px}.dt-day{background:#f1f9f5;border:1px solid #deeee6;border-radius:7px;padding:10px 5px;text-align:center;font-size:11px;color:#416658}.dt-day b{display:block;color:#08734e;margin-top:10px}
    </style>''', unsafe_allow_html=True)
    st.subheader('Duruş Yönetimi')
    top = st.container()
    controls = st.columns([1.6,1,1.2,1,1.3,.85])
    with controls[0]:
        dates = st.date_input('Tarih aralığı', value=(date.today()-timedelta(days=6),date.today()), key='dt_dates')
    with controls[1]:
        machine = st.selectbox('Makine', ['Tümü']+sorted(rows.machine_code.dropna().unique()), key='dt_machine')
    with controls[2]:
        reason = st.selectbox('Neden', ['Tümü']+sorted(rows.reason.unique()), key='dt_reason')
    with controls[3]:
        shift = st.selectbox('Vardiya', ['Tümü','Sabah','Akşam','Gece','Bilinmiyor'], key='dt_shift')
    with controls[4]:
        search = st.text_input('Kayıt ara', placeholder='Makine veya neden', key='dt_search')
    with controls[5]:
        st.write('')
        if st.button('＋ Yeni kayıt', key='dt_add', use_container_width=True):
            st.session_state['dt_new'] = True
    all_dates = st.checkbox('Tarihsiz kayıtlar dahil tüm tarihler', key='dt_all_dates')
    if not isinstance(dates,(tuple,list)):
        dates = (dates, dates)
    start, end = (dates[0], dates[-1]) if dates else (date.today(),date.today())
    data = rows.copy()
    if not all_dates:
        data = data[data._time.between(pd.Timestamp(start),pd.Timestamp(end)+pd.Timedelta(days=1),inclusive='left')]
    if machine != 'Tümü': data = data[data.machine_code == machine]
    if reason != 'Tümü': data = data[data.reason == reason]
    if shift != 'Tümü': data = data[data.Vardiya == shift]
    if search.strip():
        data = data[(data.machine_code.fillna('')+' '+data.reason).str.contains(search.strip(),case=False,regex=False)]
    total = data.duration.sum()
    failures = data[data.reason.str.contains('arıza',case=False,regex=False)]
    completed = failures[failures.end_time.fillna('').str.strip().ne('')]
    mttr = completed.duration.mean() if len(completed) else None
    with top:
        cols = st.columns(5)
        values = [('Toplam Duruş Süresi',f'{total:,.1f} dk'),('Duruş Sayısı',str(len(data))),('Ortalama Duruş',f'{data.duration.mean():.1f} dk' if len(data) else '—'),('En Uzun Duruş',f'{data.duration.max():.1f} dk' if len(data) else '—'),('MTTR · tamamlanan arızalar',f'{mttr:.1f} dk' if mttr is not None else '—')]
        for col,(label,value) in zip(cols,values):
            col.markdown(f'<div class="dt-kpi"><small>{label}</small><b>{value}</b></div>',unsafe_allow_html=True)

    def heading(title):
        st.markdown(f'<div class="dt-heading">{title}</div>',unsafe_allow_html=True)

    def chart(fig,key,height=210):
        fig.update_layout(height=height,margin=dict(l=12,r=18,t=20,b=35),font=dict(size=11,color='#315846'),paper_bgcolor='rgba(0,0,0,0)',plot_bgcolor='rgba(0,0,0,0)',legend=dict(orientation='h',font=dict(size=10)))
        st.plotly_chart(fig,use_container_width=True,config={'displayModeBar':False},key=key)

    def bars(series,unit='dk'):
        if series.empty:
            st.caption('Seçili filtrede kayıt yok.')
        maximum = max(float(series.max()),1) if len(series) else 1
        for label,value in series.items():
            st.markdown(f'<div class="dt-bar"><span>{html.escape(str(label))}</span><div class="dt-track"><i style="width:{float(value)/maximum*100:.1f}%"></i></div><span>{value:.1f} {unit}</span></div>',unsafe_allow_html=True)

    reasons = data.groupby('reason').duration.sum().sort_values(ascending=False)
    first = st.columns([1,1.15,1])
    with first[0],st.container(border=True):
        heading('Duruş Nedenleri Dağılımı')
        if total > 0:
            fig = go.Figure(go.Pie(labels=reasons.index,values=reasons.values,hole=.7,textinfo='none',marker=dict(colors=['#0a9966','#f3b842','#ed7074','#469ecd','#89b8a1'])))
            fig.add_annotation(x=.5,y=.5,text=f'<b>{total:.1f} dk</b>',showarrow=False)
            chart(fig,'dt_donut')
        else: st.caption('Seçili filtrede duruş süresi yok.')
    with first[1],st.container(border=True):
        heading('Pareto Analizi')
        if total > 0:
            fig=go.Figure(go.Bar(x=reasons.index,y=reasons.values,name='Süre (dk)',marker_color='#10966a'))
            fig.add_trace(go.Scatter(x=reasons.index,y=reasons.cumsum()/total*100,yaxis='y2',name='Kümülatif %',mode='lines+markers',line=dict(color='#24556a')))
            fig.update_layout(yaxis2=dict(overlaying='y',side='right',range=[0,110],ticksuffix='%'),yaxis=dict(title='Dakika'))
            chart(fig,'dt_pareto')
        else: st.caption('Analiz için kayıt bulunmuyor.')
    with first[2]:
        with st.container(border=True):
            heading('Makine Bazlı Duruş Süreleri')
            bars(data.groupby('machine_code').duration.sum().sort_values(ascending=False).head(6))
        with st.container(border=True):
            heading('Vardiya Bazlı Duruş')
            bars(data.groupby('Vardiya').duration.sum())
            st.caption('Vardiya, kaydın başlangıç saatine göre belirlenir.')

    with st.container(border=True):
        heading('Son Duruşlar')
        view=data[['machine_code','reason','event_at','start_time','end_time','duration','notes']].rename(columns={'machine_code':'Makine','reason':'Neden','event_at':'Tarih','start_time':'Başlangıç','end_time':'Bitiş','duration':'Süre (dk)','notes':'Açıklama'})
        st.dataframe(
            view.round(2),
            hide_index=True,
            use_container_width=True,
            height=280,
            column_config={
                'Makine': st.column_config.TextColumn(width='small'),
                'Neden': st.column_config.TextColumn(width='medium'),
                'Tarih': st.column_config.DatetimeColumn(format='DD.MM.YYYY HH:mm:ss', width='medium'),
                'Başlangıç': st.column_config.TextColumn(width='medium'),
                'Bitiş': st.column_config.TextColumn(width='medium'),
                'Süre (dk)': st.column_config.NumberColumn(format='%.2f', width='small'),
                'Açıklama': st.column_config.TextColumn(width='large'),
            },
        )
    with st.container(border=True):
        heading('Makine Durumu · Anlık')
        selected_machines=machines if machine=='Tümü' else machines[machines.machine_code==machine]
        st.dataframe(
            selected_machines[['machine_code','status']].rename(columns={'machine_code':'Makine','status':'Durum'}),
            hide_index=True,
            use_container_width=True,
            height=170,
            column_config={
                'Makine': st.column_config.TextColumn(width='medium'),
                'Durum': st.column_config.TextColumn(width='large'),
            },
        )
    with st.container(border=True):
        heading('MTBF / MTTR Detayları')
        mttr_col, mtbf_col = st.columns(2)
        with mttr_col:
            st.metric('MTTR',f'{mttr:.1f} dk' if mttr is not None else '—')
            st.caption(f'{len(completed)} bitiş bilgisi olan arıza kaydından hesaplandı.')
        with mtbf_col:
            st.metric('MTBF','—')
            st.caption('Seçili döneme ait doğrulanmış çalışma süresi bulunmadığından hesaplanmadı.')

    third=st.columns([1.45,1,1])
    with third[0],st.container(border=True):
        heading('Duruş Takvimi · Son 7 Gün')
        daily=data.groupby(data._time.dt.date).duration.sum()
        cells=[]
        for offset in range(6,-1,-1):
            day=end-timedelta(days=offset)
            cells.append(f'<div class="dt-day">{day:%d.%m}<b>{daily.get(day,0):.1f} dk</b></div>')
        st.markdown('<div class="dt-days">'+''.join(cells)+'</div>',unsafe_allow_html=True)
    with third[1],st.container(border=True):
        heading('En Fazla Duruş Olan Makineler')
        bars(data.groupby('machine_code').size().sort_values(ascending=False).head(4),'kayıt')
    with third[2],st.container(border=True):
        heading('Hızlı İşlemler')
        if st.button('＋ Yeni Duruş Kaydı',key='dt_quick_new',use_container_width=True,type='primary'):
            st.session_state['dt_new']=True
        st.download_button('Duruş Raporunu İndir',view.to_csv(index=False).encode('utf-8-sig'),file_name='durus_raporu.csv',mime='text/csv',use_container_width=True)
        with st.expander('Duruş analizi'):
            if total:
                st.write(f'En büyük neden: {reasons.index[0]} · {reasons.iloc[0]:.1f} dk (%{reasons.iloc[0]/total*100:.1f}).')
            else: st.write('Bu filtrede analiz edilecek kayıt yok.')
