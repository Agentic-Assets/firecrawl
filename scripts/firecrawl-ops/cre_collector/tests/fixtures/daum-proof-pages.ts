
export function daumProofPage(total=100,page=1,allSale=false,reverse=false):string {
 const indices=Array.from({length:total},(_,i)=>i).slice((page-1)*90,page*90);
 if(reverse)indices.reverse();
 const rows=indices.map(i=>({title:"Synthetic "+i,address:"Phoenix, AZ 85001",single_post_link:"https://daumcommercial.com/property/Synthetic-"+i+"/","link-to":"https://daumcommercial.com/property/Synthetic-"+i+"/",lat:"33",lng:"-112"}));
 return '<html><div class="results-list-controls"><div class="results">'+total+' Results Showing</div></div><div class="results-list wrap-row">'+indices.map(i=>'<div class="item"><a class="photo" href="https://daumcommercial.com/property/Synthetic-'+i+'/"></a><div class="content"><p class="listing-type">'+(allSale?'Sale':i%3===0?'Sale':i%3===1?'Lease':'Sale or Lease')+'</p><a class="red-text" href="https://daumcommercial.com/property/Synthetic-'+i+'/">Synthetic '+i+'</a><p class="info-location">Phoenix <span>|</span> AZ <span>|</span> 85001</p></div><div class="content"><p>12,500 SF</p><div class="sale-price">$3,250,000</div></div></div>').join('')+'</div><script>var propertySearchData = '+JSON.stringify(rows)+';</script></html>';
}
export function daumProofDetail(i:number,allSale=false):string {
 return '<html><head><link rel="canonical" href="https://daumcommercial.com/property/Synthetic-'+i+'/"><link rel="shortlink" href="https://daumcommercial.com/?p='+(50000+i)+'"></head><body><div class="main-section"><div class="title-property">Synthetic '+i+'</div><div class="title-about">'+(allSale?'Sale':i%3===0?'Sale':i%3===1?'Lease':'Sale or Lease')+'</div><ul class="state-city"><li>Phoenix</li><li>AZ</li><li>85001</li></ul><div class="info-wrap"><div class="info-units">12,500 SF</div><div class="info-units">$3,250,000</div></div></div></body></html>';
}
