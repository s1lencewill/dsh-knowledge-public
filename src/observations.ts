import { createHash } from 'node:crypto';
/** Local, bounded range accounting. Contains no semantic judgment or source text. */
export class EvidenceObservations {
  private sessions=new Map<string, Map<string, [number,number][]>>();
  observe(session:string,value:any,callId:string):any {
    if(!value || typeof value!=='object' || Array.isArray(value)) return value;
    if(!this.sessions.has(session)){if(this.sessions.size>=500)this.sessions.delete(this.sessions.keys().next().value!);this.sessions.set(session,new Map());}
    const seen=this.sessions.get(session)!;let newDocuments=0,newRanges=0;
    const decorate=(item:any)=>{
      if(!item || typeof item!=='object')return item;
      const copy={...item},card=item.sourceCard;
      const version=item.sourceVersion ?? card?.sourceVersion;
      const path=item.relativePath ?? card?.relativePath ?? card?.fileName;
      const start=item.startChar ?? card?.location?.startChar,end=item.endChar ?? card?.location?.endChar;
      if(typeof version==='string' && typeof path==='string' && Number.isInteger(start) && Number.isInteger(end) && end>=start){
        const key=JSON.stringify([value.libraryId ?? card?.libraryId,path,version]);
        let ranges=seen.get(key);if(!ranges){newDocuments++;ranges=[];if(seen.size>=200)seen.delete(seen.keys().next().value!);seen.set(key,ranges);}
        if(!ranges.some(([a,b])=>start>=a && end<=b)){newRanges++;ranges.push([start,end]);if(ranges.length>100)ranges.shift();}
        const citationId='ksrc-'+createHash('sha256').update(JSON.stringify([callId,key,start,end])).digest('hex').slice(0,16);
        copy.citationId=citationId;
        if(card)copy.sourceCard={...card,citationId};
      }
      return copy;
    };
    let result=decorate(value);
    for(const name of ['evidence','documents','entries'])if(Array.isArray(value[name]))result[name]=value[name].map(decorate);
    result.novelty={scope:'session',newDocuments,newRanges,countedAs:'bounded observed document versions and noncontained ranges; not semantic novelty or support'};
    return result;
  }
}
