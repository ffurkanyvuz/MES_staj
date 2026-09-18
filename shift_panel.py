"""Shift comparisons from recorded counter increments, without invented history."""
from datetime import date, timedelta
import io
import html
import pandas as pd
import plotly.graph_objects as go
import streamlit as st


def counter_history(frame):
    frame = frame.copy()
    frame['timestamp'] = pd.to_datetime(frame.timestamp, errors='coerce')
    frame['quantity'] = pd.to_numeric(frame.quantity, errors='coerce')
    frame = frame.sort_values(['machine_code', 'timestamp', 'id'])
    previous = frame.groupby('machine_code').quantity.shift()
    # First observation is a baseline; a counter reset does not imply new output.
    frame['Üretim'] = (frame.quantity-previous).clip(lower=0).fillna(0)
    return frame


def render_shift_panel(query, schedule):
    names = list(schedule)
    colors = dict(zip(names, ['#0c9767','#eeaa32','#349cce']))
    history = counter_history(query('SELECT id,machine_code,quantity,timestamp,shift FROM production_history ORDER BY machine_code,timestamp,id'))
    assignments = query('SELECT shift_name,machine_code,operator_name FROM shift_machine_assignments WHERE active=1 ORDER BY machine_code,shift_name')
    stops = query('SELECT machine_code,reason,duration,event_at FROM downtime')
    machines = query('SELECT machine_code,product FROM machines ORDER BY machine_code')
    st.subheader('Vardiya Yönetimi')
    st.markdown('''<style>
    .sh-card{background:linear-gradient(115deg,#fff,#edf9f4);border:1px solid #d3ebe0;border-radius:9px;padding:13px;min-height:105px;box-sizing:border-box}
    .sh-card small{display:block;font-size:12px;color:#416657}.sh-card b{display:block;color:#075b40;font-size:24px;line-height:34px}.sh-card span{font-size:11px;color:#648273}
    .sh-title{font-size:14px;color:#12583e;font-weight:700;margin-bottom:10px}
    </style>''',unsafe_allow_html=True)
    kpi_area = st.container()
    filters = st.columns([1.5,1,1,1.3])
    with filters[0]: dates=st.date_input('Tarih aralığı',(date.today()-timedelta(days=6),date.today()),key='sh_dates')
    with filters[1]: chosen=st.selectbox('Vardiya',['Tümü']+names,key='sh_shift')
    with filters[2]: machine=st.selectbox('Makine',['Tümü']+machines.machine_code.dropna().tolist(),key='sh_machine')
    with filters[3]: search=st.text_input('Makine / operatör ara',key='sh_search')
    if not isinstance(dates,(tuple,list)): dates=(dates,dates)
    start,end=(dates[0],dates[-1]) if dates else (date.today(),date.today())
    data=history[history.timestamp.between(pd.Timestamp(start),pd.Timestamp(end)+pd.Timedelta(days=1),inclusive='left')].copy()
    codes=set(machines.machine_code)
    if machine!='Tümü': codes &= {machine}
    if search.strip():
        matches=assignments[(assignments.machine_code.fillna('')+' '+assignments.operator_name.fillna('')).str.contains(search.strip(),case=False,regex=False)]
        codes &= set(matches.machine_code)
    data=data[data.machine_code.isin(codes)]
    assignments=assignments[assignments.machine_code.isin(codes)]
    stops=stops[stops.machine_code.isin(codes)].copy()
    stops['_time']=pd.to_datetime(stops.event_at,errors='coerce')
    stops=stops[stops._time.between(pd.Timestamp(start),pd.Timestamp(end)+pd.Timedelta(days=1),inclusive='left')]
    def shift_for_hour(hour):
        for name,(a,b) in schedule.items():
            if a<=hour<b: return name
        return None
    stops['shift']=stops._time.dt.hour.map(shift_for_hour)
    stops['duration']=pd.to_numeric(stops.duration,errors='coerce').fillna(0).clip(lower=0)
    selected_names=names if chosen=='Tümü' else [chosen]
    data=data[data['shift'].isin(selected_names)]
    stops=stops[stops['shift'].isin(selected_names)]
    assignments=assignments[assignments.shift_name.isin(selected_names)]
    summary=pd.DataFrame({'Vardiya':selected_names})
    counts=data.groupby('shift')['Üretim'].sum()
    summary['Üretim']=summary.Vardiya.map(counts).fillna(0).astype(int)
    summary['Duruş (dk)']=summary.Vardiya.map(stops.groupby('shift').duration.sum()).fillna(0).round(2)
    summary['Operatörler']=summary.Vardiya.map(assignments.groupby('shift_name').operator_name.agg(lambda s:', '.join(sorted(set(s.dropna()))))).fillna('Atanmadı')
    summary['Hedef']='—'
    summary['OEE']='—'
    with kpi_area:
        cols=st.columns(5)
        for col,name in zip(cols,names):
            a,b=schedule[name]
            value=f'{int(counts.get(name,0)):,}' if name in selected_names else '—'
            col.markdown(f'<div class="sh-card"><small>{name} Vardiyası</small><b>{value}</b><span>{a:02d}:00–{b%24:02d}:00 · Kayıtlı üretim</span></div>',unsafe_allow_html=True)
        cols[3].markdown(f'<div class="sh-card"><small>Toplam Üretim</small><b>{int(summary["Üretim"].sum()):,}</b><span>Seçili tarih aralığı · adet</span></div>',unsafe_allow_html=True)
        cols[4].markdown('<div class="sh-card"><small>Ortalama OEE</small><b>—</b><span>Vardiya bazlı geçmiş gerekli</span></div>',unsafe_allow_html=True)
    st.caption('Üretim, ardışık sayaç kayıtlarının artışından hesaplanır. İlk kayıt başlangıç kabul edilir. Vardiya bazlı hedef ve OEE geçmişi henüz kaydedilmiyor.')

    def title(text): st.markdown(f'<div class="sh-title">{text}</div>',unsafe_allow_html=True)
    def chart(fig,key):
        fig.update_layout(height=215,margin=dict(l=12,r=12,t=15,b=35),font=dict(size=11,color='#315d4c'),paper_bgcolor='rgba(0,0,0,0)',plot_bgcolor='rgba(0,0,0,0)',legend=dict(orientation='h'))
        st.plotly_chart(fig,key=key,use_container_width=True,config={'displayModeBar':False})
    row=st.columns([1.4,1,1.2])
    with row[0],st.container(border=True):
        title('Vardiya Performansı · Üretim')
        chart(go.Figure(go.Bar(x=summary.Vardiya,y=summary['Üretim'],marker_color=[colors[n] for n in selected_names],text=summary['Üretim'],textposition='auto')),'sh_performance')
    with row[1],st.container(border=True):
        title('Vardiya Dağılımı (Üretim)')
        if summary['Üretim'].sum():
            chart(go.Figure(go.Pie(labels=summary.Vardiya,values=summary['Üretim'],hole=.68,marker_colors=[colors[n] for n in selected_names],textinfo='percent')),'sh_donut')
        else: st.info('Bu filtrede kayıtlı üretim artışı yok.')
    with row[2],st.container(border=True):
        title('Makine – Vardiya Eşleşmesi')
        if assignments.empty: st.caption('Aktif atama bulunmuyor.')
        else:
            matrix=assignments.pivot_table(index='machine_code',columns='shift_name',values='operator_name',aggfunc=lambda x:', '.join(x.dropna().astype(str))).reindex(columns=selected_names).fillna('—')
            matrix.index.name='Makine'
            st.dataframe(matrix,use_container_width=True,height=215)
        st.caption('Güncel planlı operatör atamaları.')
    row=st.columns([1.4,1,1.2])
    with row[0],st.container(border=True):
        title('Vardiya Bazlı Detaylar')
        st.dataframe(summary,hide_index=True,use_container_width=True,height=215)
    with row[1],st.container(border=True):
        title('Vardiya Bazında OEE')
        st.info('Dönemsel OEE için vardiya çalışma süresi, ideal çevrim ve hatalı üretim geçmişi gerekiyor.')
        st.caption('Mevcut anlık makine OEE değerleri geçmiş vardiyalara atanmadı.')
    with row[2],st.container(border=True):
        title('Vardiya Bazlı Üretim Trendi')
        trend=data.groupby([data.timestamp.dt.floor('h'),'shift'])['Üretim'].sum().reset_index()
        fig=go.Figure()
        for name in selected_names:
            part=trend[trend['shift']==name]
            fig.add_trace(go.Scatter(x=part.timestamp,y=part['Üretim'],name=name,mode='lines+markers',line=dict(color=colors[name])))
        chart(fig,'sh_trend')
    row=st.columns([1.4,1,1.2])
    with row[0],st.container(border=True):
        title('Vardiya Özet Tablo')
        st.dataframe(summary[['Vardiya','Üretim','Duruş (dk)']],hide_index=True,use_container_width=True)
    with row[1],st.container(border=True):
        title('Vardiya Bazlı Duruş Nedenleri')
        grouped=stops.groupby(['reason','shift']).duration.sum().reset_index()
        fig=go.Figure()
        for name in selected_names:
            part=grouped[grouped['shift']==name]
            fig.add_trace(go.Bar(y=part.reason,x=part.duration,name=name,orientation='h',marker_color=colors[name]))
        fig.update_layout(barmode='stack')
        chart(fig,'sh_stops')
    with row[2],st.container(border=True):
        title('Hızlı İşlemler')
        st.download_button('Vardiya Raporu · CSV',summary.to_csv(index=False).encode('utf-8-sig'),'vardiya_raporu.csv',mime='text/csv',use_container_width=True)
        output=io.BytesIO()
        with pd.ExcelWriter(output,engine='openpyxl') as writer:
            summary.to_excel(writer,sheet_name='Vardiya Özeti',index=False)
            data.to_excel(writer,sheet_name='Üretim Kayıtları',index=False)
            assignments.to_excel(writer,sheet_name='Güncel Atamalar',index=False)
        st.download_button('Excel’e Aktar',output.getvalue(),'vardiya_raporu.xlsx',mime='application/vnd.openxmlformats-officedocument.spreadsheetml.sheet',use_container_width=True)
        with st.expander('Detaylı analiz'):
            st.write(f'{len(data)} sayaç kaydı, {len(stops)} duruş kaydı incelendi.')
            st.caption('Duruşlar başlangıç saatinin vardiyasına atanır; atamalar güncel planı gösterir.')
