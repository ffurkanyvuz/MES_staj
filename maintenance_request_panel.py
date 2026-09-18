"""Maintenance request dashboard; mutations stay in the existing authorized forms."""
from datetime import date, timedelta
import html
import pandas as pd
import plotly.graph_objects as go
import streamlit as st


def render_request_panel(query):
    records=query('SELECT id,machine_code,request_type,description,priority,requested_by,requested_at,assigned_to,status,completed_at FROM maintenance_requests ORDER BY id DESC')
    machines=query('SELECT machine_code,status,last_maintenance FROM machines ORDER BY machine_code')
    maintenance=query("SELECT machine_code,maintenance_type,next_date,status FROM maintenance WHERE status!='Tamamlandı' ORDER BY next_date")
    records['_time']=pd.to_datetime(records.requested_at,errors='coerce')
    st.subheader('Bakım Talepleri')
    st.markdown('''<style>
    .mr-stat{background:linear-gradient(120deg,#fff,#eff9f5);border:1px solid #d6ebe2;border-left:4px solid var(--c);border-radius:9px;padding:12px 15px;min-height:95px;box-sizing:border-box}
    .mr-stat span{display:block;color:#426454;font-size:12px}.mr-stat b{display:block;color:#12543d;font-size:27px;line-height:36px}
    .mr-title{font-size:14px;font-weight:700;color:#12583e;margin-bottom:10px}
    .mr-item{padding:9px 10px;border-left:3px solid #e36f73;background:#fff7f6;margin:7px 0;border-radius:5px;color:#214e3a;font-size:12px;overflow-wrap:anywhere}
    .mr-detail{background:#f1faf5;border:1px solid #d7ebe0;border-radius:8px;padding:12px;color:#315844;font-size:13px;overflow-wrap:anywhere}
    </style>''',unsafe_allow_html=True)
    summary_area=st.container()
    filters=st.columns([1.5,1,1,1,1.1,1.3,.8])
    with filters[0]: dates=st.date_input('Tarih aralığı',(date.today()-timedelta(days=6),date.today()),key='mr_dates')
    with filters[1]: machine=st.selectbox('Makine',['Tümü']+sorted(machines.machine_code.dropna().unique()),key='mr_machine')
    with filters[2]: priority=st.selectbox('Öncelik',['Tümü','Kritik','Yüksek','Normal','Düşük'],key='mr_priority')
    with filters[3]: status=st.selectbox('Talep durumu',['Tümü','Açık','Atandı','Bakımda','Tamamlandı'],key='mr_status')
    with filters[4]: kind=st.selectbox('Bakım türü',['Tümü']+sorted(records.request_type.dropna().unique()),key='mr_kind')
    with filters[5]: search=st.text_input('Talep ara',placeholder='Talep no veya açıklama',key='mr_search')
    with filters[6]:
        st.write('')
        if st.button('＋ Yeni Talep',key='mr_new_button',type='primary',use_container_width=True): st.session_state['mr_new']=True
    all_dates=st.checkbox('Tüm tarihleri göster',key='mr_all_dates')
    if not isinstance(dates,(tuple,list)): dates=(dates,dates)
    start,end=(dates[0],dates[-1]) if dates else (date.today(),date.today())
    data=records.copy()
    if not all_dates: data=data[data._time.between(pd.Timestamp(start),pd.Timestamp(end)+pd.Timedelta(days=1),inclusive='left')]
    for column,value in [('machine_code',machine),('priority',priority),('status',status),('request_type',kind)]:
        if value!='Tümü': data=data[data[column]==value]
    data['Talep No']=data.id.map(lambda n:f'MT-{int(n):03d}')
    if search.strip(): data=data[(data['Talep No']+' '+data.machine_code.fillna('')+' '+data.description.fillna('')).str.contains(search.strip(),case=False,regex=False)]
    active=data[data.status!='Tamamlandı']
    critical=active[active.priority=='Kritik']
    with summary_area:
        cols=st.columns(5)
        metrics=[('Toplam Talep',len(data),'#169b68'),('Açık Talep',int((data.status=='Açık').sum()),'#169b68'),('Devam Eden',int(data.status.isin(['Atandı','Bakımda']).sum()),'#379fb7'),('Kritik',len(critical),'#df626a'),('Tamamlanan',int((data.status=='Tamamlandı').sum()),'#169b68')]
        for col,(title,value,color) in zip(cols,metrics):
            col.markdown(f'<div class="mr-stat" style="--c:{color}"><span>{title}</span><b>{value}</b></div>',unsafe_allow_html=True)

    def title(text): st.markdown(f'<div class="mr-title">{text}</div>',unsafe_allow_html=True)
    def chart(fig,key):
        fig.update_layout(height=200,margin=dict(l=10,r=10,t=20,b=35),font=dict(size=10,color='#375c49'),paper_bgcolor='rgba(0,0,0,0)',plot_bgcolor='rgba(0,0,0,0)',legend=dict(orientation='h',font=dict(size=9)))
        st.plotly_chart(fig,key=key,use_container_width=True,config={'displayModeBar':False})

    left,right=st.columns([2.7,1],gap='small')
    with left:
        with st.container(border=True):
            title('Bakım Talepleri')
            view=data[['Talep No','machine_code','request_type','description','priority','requested_at','assigned_to','status']].rename(columns={'machine_code':'Makine','request_type':'Tür','description':'Açıklama','priority':'Öncelik','requested_at':'Oluşturulma','assigned_to':'Atanan','status':'Durum'}).fillna('—')
            st.dataframe(view,hide_index=True,use_container_width=True,height=300)
            st.caption(f'{len(data)} kayıt · Liste ve grafikler seçili filtreleri kullanır.')
        panels=st.columns(4,gap='small')
        with panels[0],st.container(border=True):
            title('Makine Bazlı Talep')
            counts=data.groupby('machine_code').size()
            chart(go.Figure(go.Bar(x=counts.index,y=counts.values,marker_color='#139969',text=counts.values,textposition='auto')),'mr_machine_count')
        with panels[1],st.container(border=True):
            title('Talep Durumu Dağılımı')
            counts=data.groupby('status').size()
            if len(counts): chart(go.Figure(go.Pie(labels=counts.index,values=counts.values,hole=.65,textinfo='none',marker_colors=['#efa937','#3b9ccd','#1a9b66','#9ac8b6'])),'mr_status_pie')
            else: st.caption('Talep kaydı yok.')
        with panels[2],st.container(border=True):
            title('Günlük Talep Trendi')
            counts=data.groupby(data._time.dt.date).size()
            chart(go.Figure(go.Scatter(x=counts.index,y=counts.values,mode='lines+markers',line=dict(color='#109b69'),fill='tozeroy',fillcolor='rgba(16,155,105,.1)')),'mr_daily')
        with panels[3],st.container(border=True):
            title('Bakım Türleri')
            counts=data.groupby('request_type').size()
            if len(counts): chart(go.Figure(go.Pie(labels=counts.index,values=counts.values,hole=.65,textinfo='none',marker_colors=['#e77074','#ebae43','#3e9cc2','#1c9a6d'])),'mr_types')
            else: st.caption('Talep kaydı yok.')
    with right:
        with st.container(border=True):
            title('Makine Bakım Durumu · Anlık')
            shown=machines if machine=='Tümü' else machines[machines.machine_code==machine]
            st.dataframe(shown.rename(columns={'machine_code':'Makine','status':'Durum','last_maintenance':'Son Bakım'}),hide_index=True,use_container_width=True,height=165)
        with st.container(border=True):
            title('Kritik Bakım Talepleri')
            if critical.empty: st.caption('Bu filtrede açık kritik talep yok.')
            for _,record in critical.head(4).iterrows():
                st.markdown(f'<div class="mr-item"><b>{html.escape(record["Talep No"])} · {html.escape(str(record.machine_code))}</b><br>{html.escape(str(record.description or record.request_type))}</div>',unsafe_allow_html=True)
        with st.container(border=True):
            title('Yaklaşan Bakımlar · Güncel Plan')
            planned=maintenance.copy()
            planned['_next']=pd.to_datetime(planned.next_date,errors='coerce')
            if machine!='Tümü': planned=planned[planned.machine_code==machine]
            planned=planned[planned._next.notna()].sort_values('_next').head(4)
            st.dataframe(planned[['machine_code','maintenance_type','next_date']].rename(columns={'machine_code':'Makine','maintenance_type':'Bakım','next_date':'Tarih'}),hide_index=True,use_container_width=True,height=165)

    with st.container(border=True):
        title('Bakım Talebi Detayı')
        if data.empty:
            st.info('Seçili filtrede bakım talebi bulunmuyor.')
        else:
            labels={int(r.id):f'{r["Talep No"]} · {r.machine_code}' for _,r in data.iterrows()}
            if st.session_state.get('mr_focus') not in labels: st.session_state.pop('mr_focus',None)
            selected=st.selectbox('Talep seçin',list(labels),format_func=labels.get,key='mr_focus')
            record=data[data.id==selected].iloc[0]
            cols=st.columns([1.3,1,1])
            fields=[ [('Makine',record.machine_code),('Açıklama',record.description)], [('Talep eden',record.requested_by),('Atanan',record.assigned_to),('Öncelik',record.priority)], [('Bakım türü',record.request_type),('Durum',record.status),('Tamamlanma',record.completed_at)] ]
            for col,items in zip(cols,fields):
                content='<br>'.join(f'<b>{label}:</b> {html.escape(str(value)) if pd.notna(value) else "—"}' for label,value in items)
                col.markdown(f'<div class="mr-detail">{content}</div>',unsafe_allow_html=True)
            st.caption('Yetkili kullanıcılar aşağıdaki işlem formundan teknisyen atayabilir, bakımı başlatabilir veya tamamlayabilir.')
        st.download_button('Filtreli Talep Raporu',view.to_csv(index=False).encode('utf-8-sig'),'bakim_talepleri.csv',mime='text/csv')
    return data
