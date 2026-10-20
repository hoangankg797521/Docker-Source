'use strict';
importScripts('/studio/topology-v2.js?v=ha-v6-r8');
onmessage=function(event){const {key,nodes,edges,positions,options}=event.data;try{postMessage({key,routes:IPAMTopologyV2.routeAll(nodes,edges,positions,options)})}catch(error){postMessage({key,error:String(error.message||error)})}};
