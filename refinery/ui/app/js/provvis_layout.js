/*! refinery-platform-ui 2015-06-16 */

var provvisLayout=function(){var a=function(a,b,c){for(var d=[],e=function(b){b instanceof provvisDecl.Node&&c instanceof provvisDecl.ProvGraph&&(b=b.parent.parent),b.succs.values().filter(function(a){return null===a.parent||a.parent===c}).forEach(function(d){d instanceof provvisDecl.Node&&c instanceof provvisDecl.ProvGraph&&(d=d.parent.parent),d.predLinks.values().forEach(function(a){var c=null;b instanceof provvisDecl.Analysis?c=a.source instanceof provvisDecl.Analysis?a.source:a.source.parent.parent:b instanceof provvisDecl.Node&&(c=a.source),c&&c.autoId===b.autoId&&(a.l.ts.removed=!0)}),d.predLinks.values().some(function(a){return!a.l.ts.removed})||d.l.ts.removed||(a.push(d),d.l.ts.removed=!0)})},f=0;f<a.length&&b>f;){var g=a[f];d.push(g),g.l.ts.removed=!0,e(g),f++}return a.length>b?null:d},b=function(a,b){var c=0,d=[];a.forEach(function(a){if(a.preds.values().filter(function(a){a.parent===b?d.push(a):a instanceof provvisDecl.Node&&b instanceof provvisDecl.ProvGraph&&d.push(a.parent.parent)}),0===d.length)a.col=c;else{var e=c;d.forEach(function(a){a.col>e&&(e=a.col)}),a.col=e+1}})},c=function(a){var b=0,c=[];c.push([]);var d=0;return a.forEach(function(a){a.col===b?c[d].push(a):a.col<b?c[a.col].push(a):(d++,b++,c.push([]),c[d].push(a))}),c},d=function(a,b){var c=1,d=0,e=[],f=0,g=[];a.forEach(function(a){a.forEach(function(a){e=[],a.children.values().forEach(function(a,h){c=1,d=0,f=0,a.x=0,a.y=h*b.height,a.preds.empty()||(c=a.preds.size(),a.preds.values().forEach(function(a){d+=a.y+a.parent.y}),-1===e.indexOf(d/c)?(a.l.bcOrder=d/c,e.push(d/c)):(f+=.01,a.l.bcOrder=d/c+f,e.push(d/c+f)),g.push(a))}),g.sort(function(a,b){return a.l.bcOrder-b.l.bcOrder}).forEach(function(a,c){a.y=c*b.height}),g=[]})}),f=0;var h=[];a[0][0].children.values().forEach(function(a,i){a.x=0,a.y=i*b.height,d=0,c=0,a.succs.values().forEach(function(e){e.inputs.values().forEach(function(f){f.preds.values().some(function(b){return b.parent===a})&&(d+=e.parent.y+e.y+e.y/b.height/10+f.y,c++)})}),0!==c?-1===e.indexOf(d/c)?(a.l.bcOrder=d/c,e.push(d/c)):(f+=.01,a.l.bcOrder=d/c+f,e.push(d/c+f)):(a.l.bcOrder=0,h.push(a)),g.push(a)}),g.sort(function(a,b){return a.l.bcOrder-b.l.bcOrder});for(var i=0;i<h.length/2;i++)g.push(g.shift());g.forEach(function(a,c){a.y=c*b.height})},e=function(a,b){a.saNodes.forEach(function(a){var c=new dagre.graphlib.Graph;c.setGraph({rankdir:"LR",nodesep:0,edgesep:0,ranksep:0,marginx:0,marginy:0}),c.setDefaultEdgeLabel(function(){return{}}),a.children.values().forEach(function(a){c.setNode(a.autoId,{label:a.autoId,width:b.width,height:b.height})}),a.links.values().forEach(function(a){c.setEdge(a.source.autoId,a.target.autoId,{minlen:0,weight:1,width:0,height:0,labelpos:"r",labeloffset:10})}),dagre.layout(c),d3.entries(c._nodes).forEach(function(c){a.children.has(c.key)&&(a.children.get(c.key).x=parseInt(c.value.x-b.width/2,10),a.children.get(c.key).y=parseInt(c.value.y-b.height/2,10))})})},f=function(a,b){var c=new dagre.graphlib.Graph;c.setGraph({rankdir:"LR",nodesep:0,edgesep:0,ranksep:0,marginx:0,marginy:0}),c.setDefaultEdgeLabel(function(){return{}}),a.aNodes.forEach(function(a){c.setNode(a.autoId,{label:a.autoId,width:b.width,height:b.height})}),a.aLinks.forEach(function(a){c.setEdge(a.source.parent.parent.autoId,a.target.parent.parent.autoId,{minlen:1,weight:1,width:0,height:0,labelpos:"r",labeloffset:10})}),dagre.layout(c);var d=d3.entries(c._nodes);a.aNodes.forEach(function(a){a.x=parseInt(d.filter(function(b){return b.key===a.autoId.toString()})[0].value.x-b.width/2,10),a.y=parseInt(d.filter(function(b){return b.key===a.autoId.toString()})[0].value.y-b.height/2,10)})},g=function(g,h){f(g,h),e(g,h);var i=[],j=[];j.push(g.dataset);var k=a(j,g.aNodes.length,g);return null!==k?(b(k,g),j=[],j.push(g.dataset),g.aNodes.forEach(function(a){a.l.ts.removed=!1}),g.aLinks.forEach(function(a){a.l.ts.removed=!1}),k=a(j,g.aNodes.length,g),b(k,g),i=c(k),d(i,h)):console.log("Error: Graph is not acyclic!"),i};return{run:function(a,b){return g(a,b)}}}();
//# sourceMappingURL=provvis_layout.js.map