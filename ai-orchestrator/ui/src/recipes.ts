export type Recipe = {
  id: string
  title: string
  hint: string
}

export const RECIPES: Recipe[] = [
  {
    id: 'guvenlik',
    title: 'Güvenlik odaklı',
    hint: 'Girdi doğrulama, yetki sınırları, sır sızıntısı, kabuk/yol enjeksiyonu',
  },
  {
    id: 'hata',
    title: 'Hata ayıklama + regresyon testi',
    hint: 'Önce yeniden üret, kök neden, düzeltmeyi kanıtlayan test',
  },
  {
    id: 'mimari',
    title: 'Mimari uyum',
    hint: 'Mevcut kalıplar, gereksiz soyutlama yok, karar kaydı gerekiyor mu',
  },
  {
    id: 'arayuz',
    title: 'Arayüz uyumu',
    hint: 'Mac ve telefon, erişilebilirlik, React hook kuralı, metinler Türkçe',
  },
]
