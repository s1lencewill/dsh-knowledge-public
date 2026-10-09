import type { SourceCard } from './contracts.js';

/** Program-issued numbers, scoped to one host-observed turn. Never grants read access or support. */
export interface AnswerCitation {
  number: number;
  citationId: string;
  sourceVersion: string;
}
export interface AnswerCitationMap {
  version: 1;
  sessionId: string;
  turnKey: string;
  entries: AnswerCitation[];
}
export class AnswerCitationRegistry {
  private turns = new Map<string, Map<string, AnswerCitation & {identity: string}>>();
  register(sessionId: string, turnKey: string, cards: SourceCard[]): AnswerCitationMap {
    const key=JSON.stringify([sessionId,turnKey]);let entries=this.turns.get(key);
    if(!entries){entries=new Map();this.turns.set(key,entries);}
    while(this.turns.size>256)this.turns.delete(this.turns.keys().next().value!);
    const output:AnswerCitation[]=[];
    for(const card of cards){
      if(!/^ksrc-[a-f0-9]{16}$/.test(card?.citationId??'') || typeof card.excerpt!=='string' || !card.excerpt.trim()
        || typeof card.sourceVersion!=='string' || !card.sourceVersion || !card.location
        || card.citationOrigin==='catalogue')continue;
      const identity=JSON.stringify([card.libraryId,card.relativePath,card.sourceVersion,card.location,card.excerpt]);
      const citationId=card.citationId!;const previous=entries.get(citationId);
      // An inconsistent identity is not silently rebound to a similar document.
      if(previous && previous.identity!==identity)continue;
      if(!previous && entries.size>=300)continue;
      const entry=previous??{number:entries.size+1,citationId,sourceVersion:card.sourceVersion,identity};
      entries.set(citationId,entry);output.push({number:entry.number,citationId,sourceVersion:entry.sourceVersion});
    }
    return {version:1,sessionId,turnKey,entries:output};
  }
}
