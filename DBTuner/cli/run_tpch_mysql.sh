# #!/usr/bin/env bash
# # run_job_mysql.sh selectedList.txt queries_dir output MYSQL_SOCK MYSQL_PASSWORD

# # 记录脚本开始时间（毫秒）
# start_time=$(date +%s%3N)

# printf "query\tlat(ms)\n" > $3

# while read a
# do
#   {
#     tmp=$(mysql -uroot -p$5 -S$4 tpch < $2/$a | tail -n 1 )
#     query=$(echo $tmp | awk '{print $1}')
#     lat=$(echo $tmp | awk '{print $2}')

#     printf "$query\t$lat\n" >> $3
#   } &
# done < $1

# # 等待所有后台任务完成
# wait

# # 记录所有查询完成时间（毫秒）
# end_time=$(date +%s%3N)

# # 计算总执行时间
# total_time=$((end_time - start_time))

# # 追加记录总执行时间到输出文件
# printf "Total Execution Time\t$total_time ms\n" >> $3

#!/usr/bin/env bash
# run_job_mysql.sh  selectedList.txt  queries_dir   output	MYSQL_SOCK

printf "query\tlat(ms)\n" > $3

while read a
  do
  {
    tmp=$(mysql -uroot -p$5 -S$4 tpch < $2/$a | tail -n 1 )
    query=`echo $tmp | awk '{print $1}'`
    lat=`echo $tmp | awk '{print $2}'`

    printf "$query\t$lat\n" >> $3
  } &
done < $1
