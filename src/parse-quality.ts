/** Optional extraction observations only. Unknown keys are dropped; no semantic assurance. */
export interface ParseQualityObject {
 privateUseChars?: number|null; replacementChars?: number|null;
 searchableCjkRuns?: number|null; reviewPages?: number[]|null;
}
export type ParseQuality = string | ParseQualityObject | null;
export interface ParseQualityResult {value: ParseQuality;degraded:boolean;}
export function normalizeParseQuality(value: unknown): ParseQualityResult {
      if(value==null)return {value:null,degraded:false};
      if(typeof value==='string')return value.length<=512&&[...value].length<=256?{value:value.trim()||null,degraded:false}:{value:null,degraded:true};
      if(typeof value!=='object'||Array.isArray(value)||Object.getPrototypeOf(value)!==Object.prototype)return {value:null,degraded:true};
      const input=value as Record<string, unknown>;const clean: ParseQualityObject={};
      for(const key of ['privateUseChars','replacementChars','searchableCjkRuns'])if(Object.hasOwn(input,key)){const n=input[key];if(n!==null&&(typeof n!=='number'||!Number.isSafeInteger(n)||n<0))return {value:null,degraded:true};clean[key as keyof Pick<ParseQualityObject, "privateUseChars" | "replacementChars" | "searchableCjkRuns">]=n as number|null;}
      if(Object.hasOwn(input,'reviewPages')){const pages=input.reviewPages;if(pages===null)clean.reviewPages=null;else {if(!Array.isArray(pages)||pages.length>256||pages.some(n=>!Number.isSafeInteger(n)||n<1||n>100000))return {value:null,degraded:true};clean.reviewPages=[...new Set(pages)].sort((a,b)=>a-b);}}
      return {value:clean,degraded:false};
    }
export function formatParseQuality(value: unknown): string {const q=normalizeParseQuality(value).value;if(q===null)return '未记录';if(typeof q==='string')return q;const count=(key: "replacementChars" | "privateUseChars" | "searchableCjkRuns")=>q[key]==null?'未记录':String(q[key]);return '替换字符 '+count('replacementChars')+'；私用区字符 '+count('privateUseChars')+'；'+(q.reviewPages==null?'待复核页未记录':q.reviewPages.length?'待复核页 '+q.reviewPages.join('、'):'未标注待复核页')+'；连续中文串 '+count('searchableCjkRuns');}

