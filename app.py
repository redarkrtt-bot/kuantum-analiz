import streamlit as st
import numpy as np
from scipy.stats import poisson

# Tamamen ücretsiz bulut sunucuda telefona uyumlu arayüz ayarı
st.set_page_config(page_title="Quantum Edge Free v4.0", layout="centered")

st.title("🎯 QUANTUM EDGE FREE v4.0")
st.write("🔒 Tamamen Ücretsiz Geliştirici Altyapısı")

# Telefon için sadeleştirilmiş canlı girdi alanı
st.subheader("⚡ Canlı Maç Yönetim Merkezi")
mac_adi = st.text_input("Maçın Adı", "Örn: Real Madrid - Barcelona")

col1, col2 = st.columns(2)
with col1:
    canli_dakika = st.number_input("Canlı Dakika", min_value=0, max_value=90, value=15)
with col2:
    anlik_skor = st.text_input("Anlık Skor", "0-0")

olay = st.selectbox("Sahadaki Son Kritik Gelişme", 
                    ["Normal Oyun Akışı", "Ev Sahibi Gol Attı", "Deplasman Gol Attı", "Kırmızı Kart"])

# Arkada çalışan ve para istemeyen matematik motoru
# Başlangıç parametreleri (Ücretsiz havuzdan simüle edilir)
lambda_ev = 1.45 
lambda_dep = 1.60
kalan_sure = (90 - canli_dakika) / 90.0

if olay == "Ev Sahibi Gol Attı":
    lambda_ev *= 0.60
    lambda_dep *= 1.50
elif olay == "Deplasman Gol Attı":
    lambda_ev *= 1.40
    lambda_dep *= 0.60

canli_ev = lambda_ev * kalan_sure
canli_dep = lambda_dep * kalan_sure

# Mevcut skoru ayrıştırıp üzerine kalan gol olasılığını ekleme
c_ev, c_dep = map(int, anlik_skor.split('-'))
en_yuksek_p = 0
mutlak_skor = ""

for i in range(4):
    for j in range(4):
        p = poisson.pmf(i, canli_ev) * poisson.pmf(j, canli_dep)
        if p > en_yuksek_p:
            en_yuksek_p = p
            mutlak_skor = f"{c_ev + i}-{c_dep + j}"

st.markdown("---")
st.metric(label="🎯 SİSTEMİN MUTLAK CANLI SKOR TAHMİNİ", value=mutlak_skor)
st.success(f"Matematiksel Doğruluk Oranı: %{en_yuksek_p*100:.2f}")
